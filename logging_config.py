"""Swarm 统一日志系统。

设计目标（替代散落在 api/app.py 的临时配置）：
- 单一入口 setup_logging()，API / CLI / 脚本 / cron 都可调用，幂等
- 配置驱动（AppConfig.log_*）：级别、文件、轮转、JSON、控制台
- 轮转文件处理器（RotatingFileHandler）——修复 swarm.log 无限增长
- task_id / subtask_id 上下文（contextvar + filter）——并发任务日志可追踪
- 可选 JSON 结构化输出——便于 ELK / Loki 等日志聚合

用法：
    from swarm.logging_config import setup_logging, bind_task
    setup_logging()                      # 进程启动时调用一次
    with bind_task("task-123"):          # Brain/Worker 执行期绑定 task_id
        logger.info("...")               # 该作用域内所有日志自动带 task_id
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# ── task 上下文（跨协程传播）─────────────────────────────
_task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("swarm_task_id", default="")
_subtask_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("swarm_subtask_id", default="")
# P2-D：project_id 上下文——JSON 日志带 project_id，运维可按项目聚合/过滤全链日志。
_project_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("swarm_project_id", default="")

_LOGGED_TEXT_FMT = "%(asctime)s [%(levelname)s] %(name)s%(task_suffix)s: %(message)s"


class _ContextFilter(logging.Filter):
    """把当前 task_id/subtask_id 注入每条 record，供格式化使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        tid = _task_id_var.get("")
        sid = _subtask_id_var.get("")
        record.task_id = tid
        record.subtask_id = sid
        record.project_id = _project_id_var.get("")
        # 文本格式用的可读后缀，无 task 时为空
        if tid and sid:
            record.task_suffix = f" [task={tid[:8]} sub={sid}]"
        elif tid:
            record.task_suffix = f" [task={tid[:8]}]"
        else:
            record.task_suffix = ""
        return True


class _JsonFormatter(logging.Formatter):
    """结构化 JSON 行：每条日志一行 JSON，含 task 上下文。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if getattr(record, "task_id", ""):
            payload["task_id"] = record.task_id
        if getattr(record, "subtask_id", ""):
            payload["subtask_id"] = record.subtask_id
        if getattr(record, "project_id", ""):
            payload["project_id"] = record.project_id
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _PerTaskFileHandler(logging.Handler):
    """把每条带 task 上下文的日志【额外】落到 logs/<task_id>.log；沙箱相关日志
    (logger=swarm.worker.sandbox) 再单独落 logs/<task_id>.sandbox.log。

    目的：逐任务回看体验（每个任务一份独立日志 + 一份沙箱日志），独立于全局 swarm.log
    的轮转/混杂。LRU 控制同时打开的文件句柄数，避免大量并发任务 fd 泄漏。逐行 flush，
    确保跑挂/被杀也能看到已写内容。`logs/` 目录 gitignore，不进版本库。
    """

    def __init__(self, logs_dir: "Path", *, max_open: int = 32) -> None:
        super().__init__()
        self._dir = logs_dir
        self._max_open = max(4, int(max_open))
        from collections import OrderedDict
        self._files: "OrderedDict[str, object]" = OrderedDict()

    def _open(self, fname: str):
        f = self._files.get(fname)
        if f is not None:
            self._files.move_to_end(fname)
            return f
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            f = open(self._dir / fname, "a", encoding="utf-8")
        except OSError:
            return None
        self._files[fname] = f
        while len(self._files) > self._max_open:
            _, old = self._files.popitem(last=False)
            try:
                old.close()
            except OSError:
                pass
        return f

    def _write(self, fname: str, line: str) -> None:
        f = self._open(fname)
        if f is None:
            return
        try:
            f.write(line)
            f.flush()
        except OSError:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        tid = getattr(record, "task_id", "") or _task_id_var.get("")
        if not tid:
            return  # 无 task 上下文的日志只进全局 swarm.log，不落逐任务文件
        try:
            line = self.format(record) + "\n"
        except Exception:  # noqa: BLE001 — 格式化失败不应影响主流程
            return
        self._write(f"{tid}.log", line)
        if record.name.startswith("swarm.worker.sandbox"):
            self._write(f"{tid}.sandbox.log", line)

    def close(self) -> None:
        for f in list(self._files.values()):
            try:
                f.close()
            except OSError:
                pass
        self._files.clear()
        super().close()


# 幂等标记（同一进程多次调用只配置一次，除非 force）
_configured = False


def setup_logging(*, force: bool = False, console: bool | None = None) -> None:
    """配置 swarm 根 logger（幂等）。从 AppConfig.log_* 读取参数。

    在所有进程入口调用：API on_startup、CLI 入口、scripts/init_db、cron 任务。

    console: 覆盖 AppConfig.log_console。API 进程传 False（其 stdout/stderr
    已被 shell 重定向到 swarm.log，再开 console 会对同一文件双写）。
    """
    global _configured
    if _configured and not force:
        return

    from swarm.config.settings import PROJECT_ROOT, get_config

    cfg = get_config()
    level = getattr(logging, str(cfg.log_level).upper(), logging.INFO)
    use_console = cfg.log_console if console is None else console

    swarm_logger = logging.getLogger("swarm")
    swarm_logger.setLevel(level)
    # 清掉旧 handler（force 重配 / 替换 api/app.py 旧的临时 FileHandler / 防重复挂载）。
    # 用 sentinel 标记本模块挂的 handler，避免与第三方(uvicorn)的 handler 冲突。
    for h in list(swarm_logger.handlers):
        if getattr(h, "_swarm_managed", False):
            swarm_logger.removeHandler(h)
            h.close()
        else:
            swarm_logger.removeHandler(h)

    ctx_filter = _ContextFilter()
    if cfg.log_json:
        formatter: logging.Formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(_LOGGED_TEXT_FMT)

    handlers: list[logging.Handler] = []

    # 控制台 handler
    if use_console:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(formatter)
        ch.addFilter(ctx_filter)
        ch._swarm_managed = True  # type: ignore[attr-defined]
        handlers.append(ch)

    # 轮转文件 handler
    if cfg.log_file:
        log_path = Path(cfg.log_file)
        if not log_path.is_absolute():
            log_path = PROJECT_ROOT / log_path
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=cfg.log_max_bytes,
                backupCount=cfg.log_backup_count,
                encoding="utf-8",
            )
            fh.setLevel(level)
            fh.setFormatter(formatter)
            fh.addFilter(ctx_filter)
            fh._swarm_managed = True  # type: ignore[attr-defined]
            handlers.append(fh)
        except OSError as exc:
            # 文件不可写不应致命，控制台仍可用
            logging.getLogger(__name__).warning(
                "无法写日志文件 %s: %s（仅控制台输出）", log_path, exc
            )

    # 逐任务日志文件 handler（logs/<task_id>.log + <task_id>.sandbox.log）。
    # 默认开启；SWARM_PER_TASK_LOGS=false 可关。便于逐任务回看，与全局 swarm.log 并存。
    import os as _os
    if _os.environ.get("SWARM_PER_TASK_LOGS", "true").lower() not in ("false", "0", "no"):
        try:
            logs_dir = PROJECT_ROOT / "logs"
            pth = _PerTaskFileHandler(logs_dir)
            pth.setLevel(level)
            pth.setFormatter(formatter)
            pth.addFilter(ctx_filter)
            pth._swarm_managed = True  # type: ignore[attr-defined]
            handlers.append(pth)
        except Exception as exc:  # noqa: BLE001 — 逐任务日志失败不应致命
            logging.getLogger(__name__).warning("逐任务日志 handler 初始化失败: %s", exc)

    for h in handlers:
        swarm_logger.addHandler(h)
    # 不向 root 传播，避免与 uvicorn / pytest 的 root handler 重复打印
    swarm_logger.propagate = False

    # 让 uvicorn.error 也写进同一文件（与历史行为一致）
    uvicorn_logger = logging.getLogger("uvicorn.error")
    for h in handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and not any(
            isinstance(eh, logging.handlers.RotatingFileHandler)
            for eh in uvicorn_logger.handlers
        ):
            uvicorn_logger.addHandler(h)

    _configured = True


@contextmanager
def bind_task(task_id: str, subtask_id: str = "", project_id: str = "") -> Iterator[None]:
    """在作用域内绑定 task_id（含 subtask_id/project_id），该域内所有 swarm 日志自动携带。

    用 contextvars，跨 async 任务安全传播；退出作用域自动还原。project_id 可选（P2-D）。
    """
    t_token = _task_id_var.set(task_id or "")
    s_token = _subtask_id_var.set(subtask_id or "")
    p_token = _project_id_var.set(project_id or "")
    try:
        yield
    finally:
        _task_id_var.reset(t_token)
        _subtask_id_var.reset(s_token)
        _project_id_var.reset(p_token)


def set_task_context(task_id: str, subtask_id: str = "", project_id: str = "") -> None:
    """非上下文管理器形式绑定（如无法用 with 的回调里）。不会自动还原。project_id 可选（P2-D）。"""
    _task_id_var.set(task_id or "")
    _subtask_id_var.set(subtask_id or "")
    if project_id:
        _project_id_var.set(project_id)


def clear_task_context() -> None:
    _task_id_var.set("")
    _subtask_id_var.set("")
    _project_id_var.set("")


def current_task_id() -> str:
    return _task_id_var.get("")


def resolve_log_path() -> "Path | None":
    """返回当前配置的日志文件绝对路径（无文件日志时 None）。"""
    from swarm.config.settings import PROJECT_ROOT, get_config

    cfg = get_config()
    if not cfg.log_file:
        return None
    p = Path(cfg.log_file)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def read_task_logs(task_id: str, *, limit: int = 500, tail_scan: int = 200_000) -> list[str]:
    """从日志文件中提取某 task 的日志行（按 [task=<前8位>] 前缀过滤）。

    - task_id: 完整 task_id，内部用前 8 位匹配（与 _ContextFilter 前缀一致）
    - limit: 最多返回的行数（取最新 limit 行）
    - tail_scan: 先只扫主日志末尾这么多字节（快路径，避免大日志全量读）

    返回时间顺序（旧→新）的日志行列表；文件不存在或无匹配返回空列表。
    JSON 模式下匹配 "task_id": "<完整id>"。

    修复"done 任务查不到日志"：任务跑完后新日志不断写入，旧任务日志会被推出 tail_scan
    窗口 + 被 RotatingFileHandler 滚动到 .1/.2 备份文件。因此：快路径（主日志尾部）未命中时，
    自动回退扫描【主日志全文件 + 所有轮转 backup（swarm.log.1/.2...）】，确保历史任务可查。
    """
    path = resolve_log_path()
    if not path or not path.exists():
        return []

    short = (task_id or "")[:8]
    if not short:
        return []
    text_marker = f"[task={short}"      # 文本格式前缀
    json_marker = f'"task_id": "{task_id}"'  # JSON 格式字段

    def _scan_file(p, *, tail: int | None) -> list[str]:
        out: list[str] = []
        try:
            size = p.stat().st_size
            with open(p, "rb") as f:
                if tail is not None and size > tail:
                    f.seek(size - tail)
                    f.readline()  # 丢弃可能被截断的半行
                chunk = f.read().decode("utf-8", errors="replace")
            for line in chunk.splitlines():
                if text_marker in line or json_marker in line:
                    out.append(line)
        except OSError:
            return []
        return out

    # 快路径：主日志尾部 tail_scan 字节
    matched = _scan_file(path, tail=tail_scan)

    # 回退：尾部匹配【不足 limit】→ 扫全量 + 轮转 backup（治本 WebUI 任务日志不完整）。
    # 原仅在 `not matched`（零命中）才回退，对【长跑中任务】失效：其尾部总有近期匹配 →
    # matched 非空 → 永不回退 → 早期 ANALYZE/PLAN 日志一旦被挤出 tail 窗口/滚进 .1 就丢失。
    # 改为"尾部匹配 < limit 就回退"：仍能命中更早行（含轮转 backup）补齐，直到凑满 limit；
    # 尾部已 ≥limit（纯 live tail 场景）则跳过全量扫，保持高效。全量扫含 tail 区，无需去重。
    if len(matched) < limit:
        all_lines: list[str] = []
        # 轮转 backup 从旧到新：swarm.log.3 → .2 → .1 → swarm.log（时间顺序）
        import glob as _glob
        backups = sorted(
            _glob.glob(str(path) + ".*"),
            key=lambda s: int(s.rsplit(".", 1)[-1]) if s.rsplit(".", 1)[-1].isdigit() else 0,
            reverse=True,
        )
        for b in backups:
            from pathlib import Path as _P
            all_lines.extend(_scan_file(_P(b), tail=None))
        all_lines.extend(_scan_file(path, tail=None))
        matched = all_lines

    if len(matched) > limit:
        matched = matched[-limit:]
    return matched


def iter_task_log_tail(task_id: str, *, from_end_bytes: int = 50_000):
    """生成器：先吐已有匹配行（末尾 from_end_bytes 内），再持续 tail 新增匹配行。

    用于 SSE 实时流。每次 yield 一行（已 rstrip）。调用方负责节流/sleep 与
    连接断开处理。不触发任何任务执行——纯文件读。

    轮转处理：检测到文件 inode 变化（RotatingFileHandler 滚动）时重新打开。
    """
    poller = TaskLogPoller(task_id, from_end_bytes=from_end_bytes)
    import time as _time

    while True:
        batch = poller.poll()
        if batch:
            yield from batch
        else:
            _time.sleep(1.0)


class TaskLogPoller:
    """非阻塞日志轮询器：每次 poll() 返回自上次以来的新匹配行（可能为空）。

    适合 SSE：异步层控制节流，不在工作线程里 sleep（避免线程泄漏）。
    """

    def __init__(self, task_id: str, *, from_end_bytes: int = 50_000) -> None:
        import os

        self.task_id = task_id
        self.short = (task_id or "")[:8]
        self.text_marker = f"[task={self.short}"
        self.json_marker = f'"task_id": "{task_id}"'
        self.path = resolve_log_path()
        self.pos = 0
        self.inode = None
        self._primed = False
        self._from_end = from_end_bytes
        self._os = os

    def _match(self, line: str) -> bool:
        return bool(self.short) and (self.text_marker in line or self.json_marker in line)

    def _prime(self) -> list[str]:
        """首次：读末尾 from_end_bytes 的已有匹配行，并把 pos 定位到文件末尾。"""
        out: list[str] = []
        if not self.path or not self.path.exists():
            return out
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as f:
                if size > self._from_end:
                    f.seek(size - self._from_end)
                    f.readline()
                for raw in f.read().decode("utf-8", errors="replace").splitlines():
                    if self._match(raw):
                        out.append(raw)
                self.pos = f.tell()
            self.inode = self._os.stat(self.path).st_ino
        except OSError:
            pass
        return out

    def poll(self) -> list[str]:
        """返回自上次以来的新匹配行（首次含尾部回放）。无新增返回空列表。"""
        if not self._primed:
            self._primed = True
            return self._prime()
        if not self.path or not self.path.exists():
            return []
        out: list[str] = []
        try:
            cur_inode = self._os.stat(self.path).st_ino
            if self.inode is not None and cur_inode != self.inode:
                self.pos = 0  # 轮转，从头读新文件
                self.inode = cur_inode
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                chunk = f.read()
                self.pos = f.tell()
            if chunk:
                for raw in chunk.decode("utf-8", errors="replace").splitlines():
                    if self._match(raw):
                        out.append(raw)
        except OSError:
            pass
        return out

