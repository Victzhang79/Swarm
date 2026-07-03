"""远程沙箱管理器 — 基于 E2B/CubeSandbox

Swarm Worker 的执行环境不是本地 Docker，而是远程 CubeSandbox 集群。
通过 dev_sidecar 本地代理连接到 CubeAPI 控制面 + CubeProxy 数据面。

使用方式：
    from swarm.worker.sandbox import get_sandbox_manager

    manager = get_sandbox_manager()
    sandbox = manager.create(template_id="tpl-xxx")
    result = manager.run_code(sandbox, "print('hello')")
    manager.kill(sandbox.sandbox_id)
"""

from __future__ import annotations

import base64
import logging
import os
import posixpath
import re as _re
import shlex
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel

from swarm.config.settings import SandboxConfig, get_config
from swarm.project.preprocess import EXCLUDED_DIRS, EXCLUDED_EXTENSIONS

logger = logging.getLogger(__name__)

# 裸 `python` token（命令开头或管道/分隔符/&&/||/; 之后），但排除 python3 / pythonX。
# 沙箱镜像 PATH 只有 python3，无 python 别名 → 裸 python 调用须规范化为 python3。
_PYTHON_TOKEN_RE = _re.compile(r"(?<![\w./-])python(?![\w.-])")

# `py_compile` 只接受【文件】参数，传目录（如 `.`）必报 [Errno 21] Is a directory，
# exit=1 → L1 构建闸门假阴性（task d4f9db79 实证：trivial 成功却被判失败）。
# 默认 harness build_command="python -m py_compile ." 一直不可执行，被 python→python3
# 修复前的 127 错误掩盖。compileall 才接受目录并递归编译。在统一入口幂等规范化：
# 当 py_compile 的参数含非 .py 路径（目录）时整体改 compileall。与 _normalize_python_cmd
# 同源，沙箱/本地两条路径自动覆盖；LLM 即使再生成 py_compile <dir> 也被兜底改正。
# 捕获 `-m py_compile <args...>`，args 直到命令分隔符（&& || ; | 换行）或行尾。
_PY_COMPILE_RE = _re.compile(r"-m\s+py_compile\s+(?P<args>[^&|;\n]+)")


def _normalize_py_compile_cmd(command: str) -> str:
    """把 `py_compile <含目录参数>` 规范化为 `compileall <同参数>`（幂等）。

    py_compile 仅接受文件；只要参数里出现任一非 .py 结尾的路径（典型是目录 `.`），
    命令必失败。compileall 接受目录递归编译，是"编译整个项目"的正确工具。
      - "python3 -m py_compile ."              → "python3 -m compileall ."         ✅
      - "python -m py_compile src/"            → "python -m compileall src/"        ✅
      - 'python3 -m py_compile "a.py" "b.py"'  → 不变（全是 .py 文件，py_compile 正确）✅
      - "python3 -m compileall ."              → 不变（已是 compileall）             ✅
    """
    if not command or "py_compile" not in command:
        return command

    def _sub(m: "_re.Match[str]") -> str:
        raw = m.group("args")
        # 拆 token 检查：剥掉成对引号后，任一非 .py 结尾的 token 视为目录 → 需 compileall。
        toks = [t.strip("'\"") for t in raw.split() if t.strip("'\"")]
        # 仅看位置参数（跳过 -q/-f 等选项），任一不以 .py 结尾即判定含目录。
        pos = [t for t in toks if not t.startswith("-")]
        has_dir = any(not t.endswith(".py") for t in pos) if pos else True
        if has_dir:
            return m.group(0).replace("py_compile", "compileall", 1)
        return m.group(0)

    return _PY_COMPILE_RE.sub(_sub, command)


def _normalize_python_cmd(command: str) -> str:
    """把命令中独立的 `python` token 规范化为 `python3`（幂等）。

    沙箱 python 镜像 PATH 只有 python3，无 python 别名。harness/trivial 生成的
    "python -m py_compile" / "python -m pytest" 等会 exit=127 command not found，
    导致 L1 误判失败（task a4988789 实证）。在沙箱命令执行统一入口规范化，单一事实源，
    覆盖所有 harness/worker/trivial 命令，避免逐处改命令字符串"修一个漏一个"。

    用负向断言只替换独立 token：
      - "python -m pytest"   → "python3 -m pytest"   ✅
      - "python3 -m pytest"  → 不变（python3 不匹配）  ✅
      - "PYTHONPATH=. python" → "PYTHONPATH=. python3"（PYTHONPATH 不受影响）✅
      - "/usr/bin/python"    → 不变（前面有 / ，不匹配）✅
    """
    if not command or "python" not in command:
        return command
    # 先 python→python3，再 py_compile <dir>→compileall <dir>（两道幂等规范化串联，单一事实源）
    command = _PYTHON_TOKEN_RE.sub("python3", command)
    return _normalize_py_compile_cmd(command)


# A1 批3：进程级稳定实例 ID。多副本场景下，每个 swarm 进程有唯一 instance_id，
# 创建的沙箱打 metadata={"swarm_instance": <id>} 标签，启动清扫只 kill 本实例标签的
# 沙箱——多副本互不误杀（替代 12.2 的 opt-in 全清扫开关止血）。
# 优先用 SWARM_INSTANCE_ID 环境变量（容器编排可注入稳定 ID），否则进程级随机 UUID。
_INSTANCE_ID: str | None = None


def get_instance_id() -> str:
    """返回本进程的稳定实例 ID（用于沙箱归属标签）。"""
    global _INSTANCE_ID
    if _INSTANCE_ID is None:
        import uuid
        _INSTANCE_ID = os.environ.get("SWARM_INSTANCE_ID") or f"swarm-{uuid.uuid4().hex[:12]}"
    return _INSTANCE_ID

MAX_SYNC_FILE_SIZE = 1_048_576  # 1 MiB

# ── 主干A 治本：跨子任务共享的聚合清单（多并发 worker 共写，last-write-wins 互覆盖）──
# 这些文件被 N 个并行 worker 各加不同 <module>/<dependency>/Project 条目；它们的 pull-back
# 写盘必须与别的 worker 的"自产出重置→git diff"原子区串行（同一把 per-project flock），否则
# 会在那段窗口里插入污染对方 diff，导致 +<module> 从 diff 丢失、下游 MERGE 并集也救不回。
# 与 worker.workspace_manifest / brain.merge_engine._is_aggregate_manifest 覆盖的生态一致。
_SHARED_MANIFEST_BASENAMES = frozenset({
    "pom.xml", "settings.gradle", "settings.gradle.kts",
    "build.gradle", "build.gradle.kts", "cargo.toml", "go.work",
})


def _is_shared_manifest(rel_posix: str) -> bool:
    """聚合清单（多写者共享态）→ True。其 pull-back 写盘需 flock 守护，非清单文件各子任务独占免锁。"""
    base = rel_posix.rsplit("/", 1)[-1].lower()
    return base in _SHARED_MANIFEST_BASENAMES or base.endswith(".sln")


_sidecar_initialized = False
_sandbox_manager: "SandboxManager | None" = None


def get_sandbox_manager() -> "SandboxManager":
    """进程内单例 SandboxManager（API 与 Worker 共享实例追踪）"""
    global _sandbox_manager
    if _sandbox_manager is None:
        _sandbox_manager = SandboxManager(get_config().sandbox)
    return _sandbox_manager


def apply_sandbox_env(config: SandboxConfig | None = None) -> SandboxConfig:
    """将 CubeSandbox 配置写入 os.environ（必须在 setup_dev_sidecar 之前调用）"""
    cfg = config or get_config().sandbox
    os.environ["E2B_API_URL"] = cfg.api_url
    os.environ["CUBE_REMOTE_PROXY_BASE"] = cfg.proxy_base
    os.environ["CUBE_REMOTE_SANDBOX_DOMAIN"] = cfg.sandbox_domain
    os.environ["E2B_API_KEY"] = cfg.api_key
    os.environ["CUBE_REMOTE_PROXY_VERIFY_SSL"] = str(cfg.verify_ssl).lower()
    os.environ.pop("E2B_DOMAIN", None)
    logger.info(
        "Sandbox env applied: api_url=%s proxy_base=%s",
        cfg.api_url,
        cfg.proxy_base,
    )
    return cfg


def reset_sandbox_manager() -> None:
    """测试或配置重载后重置单例"""
    # TD2606-B15：先连带重置热池（drain 需 manager 仍活着 kill 池内沙箱），再清 manager 单例。
    # 否则热池单例仍持死 manager 引用、borrowed 不归零 → 后续 acquire 退化 throwaway churn。
    try:
        from swarm.worker.sandbox_pool import reset_sandbox_pool
        reset_sandbox_pool()
    except Exception:  # noqa: BLE001
        pass
    global _sandbox_manager
    if _sandbox_manager is not None:
        try:
            _sandbox_manager.kill_all()
        except Exception:
            pass
    _sandbox_manager = None


def sandbox_path(local_rel: str, remote_root: str = "/workspace") -> str:
    """将 workspace 相对路径映射为沙箱内绝对路径。

    拼接后用 posixpath.normpath 归一化，并验证结果仍在 remote_root 下，
    防止 ``../`` 目录穿越逃逸出沙箱。
    """
    root = remote_root.rstrip("/")
    rel = local_rel.lstrip("/").replace("\\", "/").strip()
    if not rel or rel == ".":
        return root
    candidate = f"{root}/{rel}"
    normalized = posixpath.normpath(candidate)
    # 防目录穿越：归一化后必须仍在 remote_root 内
    if normalized != root and not normalized.startswith(root + "/"):
        raise ValueError(
            f"sandbox_path 越界: {local_rel!r} 解析为 {normalized!r}，"
            f"不在 {root!r} 下"
        )
    return normalized


def write_file_to_sandbox(
    sandbox: Any,
    remote_path: str,
    data: bytes | str,
    manager: "SandboxManager | None" = None,
) -> None:
    """写入沙箱文件（优先 SDK files.write，失败则 run_code fallback）。"""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    if hasattr(sandbox, "files") and hasattr(sandbox.files, "write"):
        try:
            sandbox.files.write(remote_path, payload)
            return
        except Exception as exc:
            logger.warning("sandbox.files.write failed for %s: %s", remote_path, exc)
    mgr = manager or get_sandbox_manager()
    mgr._write_file_via_code(sandbox, remote_path, payload)


def read_file_from_sandbox(
    sandbox: Any,
    path: str,
    manager: "SandboxManager | None" = None,
) -> bytes | str:
    """从沙箱读取文件。

    CubeProxy 经 dev_sidecar 转发时，envd HTTP 响应可能带错误的 Content-Encoding，
    导致 E2B SDK httpx 自动解压失败（zlib incorrect header check）。
    优先用 download_url + auto_decompress=False 读取原始字节。
    """
    mgr = manager or get_sandbox_manager()
    cfg = mgr.config

    if hasattr(sandbox, "download_url"):
        try:
            import ssl
            import urllib.error
            import urllib.request

            url = sandbox.download_url(path)
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise ValueError(f"invalid download_url: {url!r}")
            req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
            if cfg.verify_ssl:
                ctx = ssl.create_default_context()
            else:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
                return resp.read()
        except Exception as exc:
            logger.warning("download_url read failed for %s: %s", path, exc)

    if hasattr(sandbox, "files") and hasattr(sandbox.files, "read"):
        for kwargs in (
            {"format": "bytes", "gzip": False},
            {"format": "bytes"},
        ):
            try:
                data = sandbox.files.read(path, **kwargs)
                if isinstance(data, str):
                    return data.encode("utf-8")
                return bytes(data)
            except TypeError:
                try:
                    data = sandbox.files.read(path, format="bytes")
                    return bytes(data) if not isinstance(data, str) else data.encode("utf-8")
                except Exception as exc:
                    logger.warning("sandbox.files.read failed for %s: %s", path, exc)
                    break
            except Exception as exc:
                logger.warning("sandbox.files.read failed for %s: %s", path, exc)
                break

    # 最终兜底：走 shell 端点(run_command + base64)，不依赖 Jupyter kernel
    # (自建语言镜像无 kernel，run_code 会 502)。base64 -w0 保证单行输出。
    if hasattr(mgr, "run_command"):
        # 先判断是否为目录：是目录直接报错(读文件接口不该读目录)，避免 cat 目录卡住
        # P0-SEC-05(b)：shell 上下文用 shlex.quote 正确转义（{path!r} 是 Python repr，
        # 对含单引号的路径不等价于 shell 引号，有残余注入面）。
        _qp = shlex.quote(path)
        cr = mgr.run_command(
            sandbox,
            f"test -f {_qp} && base64 {_qp} | tr -d '\\n' || echo __NOT_A_FILE__",
            timeout=30,
        )
        out = (cr.stdout or "").strip()
        if out == "__NOT_A_FILE__" or not out:
            raise RuntimeError(f"not a file or empty: {path}")
        try:
            return base64.b64decode(out)
        except Exception:
            # 某些 coreutils base64 不支持，回退 python(若镜像有 python)
            pass
    code = f"""
import base64
with open({path!r}, 'rb') as f:
    print(base64.b64encode(f.read()).decode())
"""
    result = mgr.run_code(sandbox, code, timeout=30)
    if not result.success or not result.stdout.strip():
        raise RuntimeError(result.error or result.stderr or f"read failed: {path}")
    return base64.b64decode(result.stdout.strip().split("\n")[-1])


def _iter_sync_candidates(local_root: Path) -> Iterator[tuple[Path, Path, str]]:
    """遍历可同步的本地文件，产出 (abs_path, rel_path, status)。"""
    for path in local_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(local_root)
        except ValueError:
            continue
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDED_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > MAX_SYNC_FILE_SIZE:
                yield path, rel, "large"
                continue
        except OSError as exc:
            logger.debug("Skip unreadable file %s: %s", path, exc)
            continue
        yield path, rel, "ok"


# ──────────────────────────────────────────────
# 沙箱管理器
# ──────────────────────────────────────────────
class SandboxUnhealthyError(RuntimeError):
    """沙箱不健康（envd 探活失败 或 连续操作失败达阈值，疑似镜像/envd 故障）。

    由 worker 捕获 → 弃用该沙箱（不归还热池）→ 子任务以明确错误失败，
    而非让 agent 在坏沙箱上空转到超时。
    """


# ── CubeMaster 真实模板清单缓存（治本：随服务器实际模板解析，杜绝死配漂移 ID）──
# 进程级 TTL 缓存：多 worker 并发 create 不必每次都打 /templates。
_SERVER_TEMPLATES_CACHE: dict[str, Any] = {"ts": 0.0, "items": None}
_SERVER_TEMPLATES_TTL = 60.0  # 秒


class SandboxManager:
    """
    管理远程 CubeSandbox 生命周期

    职责：
    - 初始化 dev_sidecar 代理（只需一次）
    - 创建/销毁沙箱实例
    - 执行代码并返回结果
    - 池化管理（预热、复用）
    """

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or get_config().sandbox
        self._instances: dict[str, Any] = {}
        # sandbox_id → {project_id, task_id, source}
        self._sandbox_meta: dict[str, dict[str, str | None]] = {}
        # sandbox_id → 连续操作失败次数（5xx/连接类）。成功清零，达阈值熔断。
        self._fail_counts: dict[str, int] = {}
        # sandbox_id → deque[{ts, kind, message, stdout?, stderr?, code?, error?}]
        # N-CW3：用 deque(maxlen) —— append+越界丢弃是 GIL 下单原子操作，杜绝
        # reaper 线程×并发 worker 交错 append+del 损坏列表。
        self._sandbox_activity: dict[str, deque[dict[str, Any]]] = {}
        self._setup_env()
        self._init_sidecar()

    def _fetch_server_templates(self, force: bool = False) -> list[dict]:
        """查 CubeMaster 实际可用模板（GET {api_url}/templates）。进程级 TTL 缓存避免每 worker 打。

        返回 [{"id","status","imageInfo"}]；任何失败返回上次缓存或 []（调用方据空判定沿用原配置、
        绝不阻断 create）。"""
        now = time.monotonic()
        cached = _SERVER_TEMPLATES_CACHE
        if not force and cached["items"] is not None and (now - cached["ts"]) < _SERVER_TEMPLATES_TTL:
            return cached["items"]
        api_url = (getattr(self.config, "api_url", "") or "").rstrip("/")
        if not api_url:
            return cached["items"] or []
        try:
            import httpx

            headers = {}
            key = getattr(self.config, "api_key", "") or ""
            if key:
                headers["X-API-KEY"] = key
            verify = bool(getattr(self.config, "verify_ssl", True))
            resp = httpx.get(f"{api_url}/templates", headers=headers, timeout=5.0, verify=verify)
            if resp.status_code != 200:
                logger.warning("[template] 查 /templates 非 200(%s)，沿用上次缓存", resp.status_code)
                return cached["items"] or []
            raw = resp.json()
            items = [
                {
                    "id": t.get("templateID") or t.get("templateId") or t.get("id"),
                    "status": t.get("status"),
                    "imageInfo": t.get("imageInfo", "") or "",
                }
                for t in raw if isinstance(t, dict)
            ]
            items = [t for t in items if t["id"]]
            cached["items"] = items
            cached["ts"] = now
            return items
        except Exception as e:  # noqa: BLE001
            logger.warning("[template] 查 /templates 失败(%s)，沿用上次缓存/原配置", e)
            return cached["items"] or []

    def _resolve_template(self, template: str | None, project_id: str | None) -> str:
        """治本：按 CubeMaster 实际可用模板解析 template，配置漂移时自愈兜底。

        背景（2026-06-29 实证）：服务器只剩 3 个 READY 模板，而 .env/DB/默认全配的是早已被
        回收的 ID（tpl-8fa882… 等）→ 每次 create 都 `130404 template not found` → worker 静默
        “降级本地执行”绕过沙箱隔离、WebUI 点创建直接失败。死配模板 ID 本质脆弱，治本=随服务器
        真实清单解析。优先级：
          ① 配置值在服务器 READY 集 → 直接用（尊重显式配置）；
          ② 否则在 READY 集挑【项目匹配】镜像（imageInfo 含 sandbox-proj-<project_id 前缀>，
             CubeMaster 按项目烤的专属镜像）；
          ③ 再否则挑任一 READY；
          ④ 拿不到服务器清单（网络等）→ 沿用配置值，不擅改（失败按原行为如实暴露）。"""
        configured = template or self.config.default_template
        servers = self._fetch_server_templates()
        if not servers:
            return configured  # 拿不到清单 → 不擅改，沿用配置
        ready = [t for t in servers if (t.get("status") or "").upper() == "READY"]
        ready_ids = {t["id"] for t in ready}
        if configured and configured in ready_ids:
            return configured
        pick = None
        if project_id:
            prefix = str(project_id)[:8]
            pick = next(
                (t["id"] for t in ready if f"sandbox-proj-{prefix}" in (t.get("imageInfo") or "")),
                None,
            )
        if not pick and ready:
            pick = ready[0]["id"]
        if pick:
            logger.warning(
                "[template] 配置模板 %s 不在 CubeMaster 可用集 %s → 自愈选用 %s（治本：随服务器真实"
                "模板，杜绝死配漂移 ID 致 create 必炸/静默降级本地）",
                configured, sorted(ready_ids), pick,
            )
            return pick
        logger.error("[template] CubeMaster 无任何 READY 模板，沿用配置 %s（create 可能失败）", configured)
        return configured

    def register_sandbox_meta(
        self,
        sandbox_id: str,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        source: str = "manual",
    ) -> None:
        self._sandbox_meta[sandbox_id] = {
            "project_id": project_id,
            "task_id": task_id,
            "source": source,
        }

    def get_sandbox_meta(self, sandbox_id: str) -> dict[str, str | None] | None:
        return self._sandbox_meta.get(sandbox_id)

    def sandboxes_for_project(self, project_id: str) -> set[str]:
        return {
            sid for sid, meta in self._sandbox_meta.items()
            if meta.get("project_id") == project_id
        }

    def sandboxes_for_task(self, task_id: str) -> set[str]:
        return {
            sid for sid, meta in self._sandbox_meta.items()
            if meta.get("task_id") == task_id
        }

    def kill_by_task(self, task_id: str) -> int:
        """销毁某任务关联的所有沙箱，返回销毁数量（任务取消/失败时释放资源）。"""
        sids = self.sandboxes_for_task(task_id)
        for sid in sids:
            self.kill(sid)
        # 同步通知热池剔除这些 sid，回退 borrowed 计数、清死引用，防账本漂移泄漏。
        self._pool_forget(sids)
        if sids:
            logger.info("kill_by_task: 任务 %s 释放 %d 个沙箱", task_id, len(sids))
        return len(sids)

    @staticmethod
    def _pool_forget(sids) -> None:
        """若热池启用，把这些 sid 从池账本剔除（外部已 kill）。无池/异常静默。"""
        if not sids:
            return
        try:
            from swarm.worker.sandbox_pool import pool_enabled
            if not pool_enabled():
                return
            from swarm.worker.sandbox_pool import get_sandbox_pool
            pool = get_sandbox_pool()
            for sid in sids:
                pool.forget(sid)
        except Exception:  # noqa: BLE001
            logger.debug("pool.forget 通知失败（不影响 kill）", exc_info=True)

    def unregister_sandbox_meta(self, sandbox_id: str) -> None:
        self._sandbox_meta.pop(sandbox_id, None)
        self._sandbox_activity.pop(sandbox_id, None)

    def append_activity(
        self,
        sandbox_id: str,
        kind: str,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        code: str = "",
        error: str | None = None,
    ) -> None:
        """记录沙箱活动（Worker 日志 / run_code 输出），供 UI 精确展示。

        同时写一份到持久化 JSONL 文件（~/.swarm/sandbox_logs/<sid>.jsonl），
        进程重启后仍可追溯（内存态会随重启清空）。失败静默不阻断主流程。
        """
        from datetime import datetime, timezone

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "message": message,
            "stdout": stdout[:8000] if stdout else "",
            "stderr": stderr[:8000] if stderr else "",
            "code": code[:2000] if code else "",
            "error": error or "",
        }
        entries = self._sandbox_activity.setdefault(sandbox_id, deque(maxlen=500))
        entries.append(entry)  # deque(maxlen) 自动丢弃最旧，无需手动 truncate（原 del 切片非原子）
        # 持久化（追加写 JSONL，便于重启后/事后 grep 追查）
        self._persist_activity(sandbox_id, entry)

    @staticmethod
    def _activity_log_dir():
        from pathlib import Path
        d = Path.home() / ".swarm" / "sandbox_logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _persist_activity(self, sandbox_id: str, entry: dict) -> None:
        """把单条活动追加到 ~/.swarm/sandbox_logs/<sid>.jsonl。失败静默。"""
        try:
            import json as _json
            fp = self._activity_log_dir() / f"{sandbox_id}.jsonl"
            with open(fp, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            logger.debug("沙箱活动持久化失败（不阻断）: %s", sandbox_id, exc_info=True)

    def get_activity(self, sandbox_id: str, limit: int = 200) -> list[dict[str, Any]]:
        entries = self._sandbox_activity.get(sandbox_id)
        if entries:
            # deque 不支持切片，先转 list 再取尾部 limit 条
            snapshot = list(entries)
            return snapshot[-limit:] if limit else snapshot
        # 内存里没有（如进程重启后）→ 从持久化 JSONL 读回
        return self._load_persisted_activity(sandbox_id, limit)

    def _load_persisted_activity(self, sandbox_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """从 JSONL 文件读回活动（内存态丢失时的兜底）。失败返回空。"""
        try:
            import json as _json
            fp = self._activity_log_dir() / f"{sandbox_id}.jsonl"
            if not fp.is_file():
                return []
            out: list[dict[str, Any]] = []
            with open(fp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(_json.loads(line))
                        except Exception:  # noqa: BLE001
                            continue
            return out[-limit:] if limit else out
        except Exception:  # noqa: BLE001
            return []

    def _setup_env(self) -> None:
        """设置环境变量（必须在 import Sandbox / setup_dev_sidecar 之前）"""
        apply_sandbox_env(self.config)

    def _init_sidecar(self) -> None:
        """初始化 dev_sidecar 本地代理（全局只需一次）"""
        global _sidecar_initialized
        if _sidecar_initialized:
            return

        try:
            project_root = Path(__file__).parent.parent
            sidecar_path = project_root / self.config.dev_sidecar_path

            if sidecar_path.exists():
                sys.path.insert(0, str(sidecar_path.parent))
                from dev_sidecar import setup_dev_sidecar

                setup_dev_sidecar()
                _sidecar_initialized = True
                logger.info("dev_sidecar initialized successfully")
            else:
                logger.warning("dev_sidecar not found at %s, skipping", sidecar_path)
        except ModuleNotFoundError as e:
            # dev_sidecar 依赖 aiohttp（仅开发期代理用，非核心运行时依赖）。
            # 缺失时优雅降级：记录告警但不崩溃（CI/精简环境无 aiohttp 时不应 raise）。
            logger.warning(
                "dev_sidecar 依赖缺失，跳过代理初始化（不影响核心功能）: %s", e
            )
        except Exception as e:
            logger.error("Failed to init dev_sidecar: %s", e)
            raise

    def create(
        self,
        template_id: str | None = None,
        timeout: int | None = None,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        source: str = "manual",
    ) -> Any:
        """创建新的沙箱实例。

        timeout = 沙箱【生命周期】秒数(到期远端自动销毁)。默认取 worker
        max_execution_time(通常 600s)——原来硬编码 60s 会导致 mvn/npm 等长构建
        跑到一半沙箱就被远端杀，后续 run_code/run_command 打到死沙箱返回 502。
        """
        from e2b_code_interpreter import Sandbox

        if timeout is None:
            try:
                from swarm.config.settings import get_config as _gc
                timeout = max(int(_gc().worker.max_execution_time), 120)
            except Exception:
                timeout = 600
        # 治本：随 CubeMaster 真实可用模板解析（配置漂移自愈兜底），杜绝死配不存在的模板 ID
        # 致 create 必炸 130404 / 静默降级本地（2026-06-29 实证根因）。
        template = self._resolve_template(template_id, project_id)
        t0 = time.monotonic()
        logger.info("Creating sandbox with template=%s project=%s timeout=%ss", template, project_id, timeout)

        # A1 批3：打实例归属标签，供启动清扫按本实例过滤（多副本互不误杀）。
        # metadata 不被 SDK 支持时降级为无标签创建（回退 12.2 开关行为）。
        _meta = {"swarm_instance": get_instance_id()}
        if project_id:
            _meta["swarm_project"] = str(project_id)
        if task_id:
            _meta["swarm_task"] = str(task_id)
        # F1（对抗复核跟进）：request_timeout = 建沙箱【HTTP 请求】超时，绑住 CubeMaster/E2B API
        # 挂起（TCP 卡死不返回）——否则 create 走 asyncio.to_thread 会占死共享线程池一个线程直到
        # 进程重启（reviewer 实证：4 任务并发 bootstrap 时可拖垮派发）。注意与 timeout(沙箱生命周期)
        # 正交。SWARM_SANDBOX_REQUEST_TIMEOUT 可调（默认 60s），<=0 关闭。
        try:
            _rt = float(os.environ.get("SWARM_SANDBOX_REQUEST_TIMEOUT", "60"))
        except ValueError:
            _rt = 60.0

        def _do_create(**extra):
            return Sandbox.create(template=template, timeout=timeout, **extra)

        # 只对【不支持该 kwarg】的 TypeError 降级；SDK 内部的 TypeError（反序列化/断言等真错）
        # 必须原样抛出，不能被误当"不支持参数"吞掉后带病创建（对抗复核 F1）。
        def _is_unsupported_kwarg(exc: TypeError) -> bool:
            return "unexpected keyword argument" in str(exc)

        try:
            sandbox = _do_create(metadata=_meta, **({"request_timeout": _rt} if _rt > 0 else {}))
        except TypeError as _e1:
            if not _is_unsupported_kwarg(_e1):
                raise
            # 旧 SDK 不支持 request_timeout → 保住 metadata（实例隔离），仅放弃请求超时。
            try:
                sandbox = _do_create(metadata=_meta)
                logger.warning("[A1] Sandbox.create 不支持 request_timeout，降级（HTTP 请求超时失效）")
            except TypeError as _e2:
                if not _is_unsupported_kwarg(_e2):
                    raise
                # 更旧 SDK 连 metadata 也不支持 → 最小签名（实例隔离失效，回退开关清扫）。
                logger.warning("[A1] Sandbox.create 不支持 metadata，降级无标签创建（实例隔离失效，回退开关清扫）")
                sandbox = _do_create()
        self._instances[sandbox.sandbox_id] = sandbox
        self.register_sandbox_meta(
            sandbox.sandbox_id,
            project_id=project_id,
            task_id=task_id,
            source=source,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        logger.info("Sandbox created: %s", sandbox.sandbox_id)
        from swarm.audit import audit

        audit(
            "sandbox_create",
            executor="Worker",
            sandbox_id=sandbox.sandbox_id,
            template_id=template,
            duration_ms=duration_ms,
        )
        self.append_activity(
            sandbox.sandbox_id,
            "worker",
            f"沙箱已创建 (source={source}, template={template}, {duration_ms}ms)"
            + (f", project={project_id}" if project_id else "")
            + (f", task={task_id}" if task_id else ""),
        )
        return sandbox

    def kill(self, sandbox_id: str) -> None:
        """销毁指定沙箱"""
        if sandbox_id in self._instances:
            try:
                self._instances[sandbox_id].kill()
                logger.info("Sandbox killed: %s", sandbox_id)
                from swarm.audit import audit

                audit("sandbox_destroy", executor="Worker", sandbox_id=sandbox_id)
            except Exception as e:
                logger.warning("Failed to kill sandbox %s: %s", sandbox_id, e)
            finally:
                del self._instances[sandbox_id]
                self.unregister_sandbox_meta(sandbox_id)
        else:
            try:
                from e2b_code_interpreter import Sandbox as _Sandbox

                sb = _Sandbox.connect(sandbox_id)
                sb.kill()
                logger.info("Sandbox killed via server connect: %s", sandbox_id)
            except Exception as e:
                logger.warning("Failed to kill sandbox %s via connect: %s", sandbox_id, e)

    def kill_all(self) -> None:
        """销毁所有活跃沙箱"""
        for sid in list(self._instances.keys()):
            self.kill(sid)

    def clean_workspace(self, sandbox: Any, workdir: str = "/workspace") -> bool:
        """清空沙箱工作区内容（复用沙箱前/归还后调用，防跨任务/跨项目文件污染）。

        A2 批2：除 workdir 外，扩展清理 /tmp 与 $HOME 下常见缓存目录——防跨项目泄漏
        （旧实现只清 workdir，残留 /tmp、pip/npm/cargo 缓存可能被下个项目看到）。
        保守策略：清缓存与临时文件，不动 shell 配置（.bashrc 等），避免坏环境。

        走 shell 端点(commands.run)——不依赖 Jupyter kernel(自建语言镜像无 kernel)。
        返回是否成功；失败记日志不抛(调用方据返回决定是否仍复用)。
        """
        sid = getattr(sandbox, "sandbox_id", None) or str(sandbox)
        # 1) 清 workdir 内容（保留目录本身）
        # 2) 清 /tmp 内容
        # 3) 清 $HOME 下常见缓存，保留 shell 配置
        # ⚠️ 关键：绝不清 .m2/.gradle —— 这是项目专属模板（方案 B）warmup 预热的依赖缓存，
        #    是宝贵资产。误删会导致 worker 跑 mvn/gradle 时重新在线下载几十个 jar（实测
        #    清 .m2 后沙箱每次编译下载 60+ 依赖，warmup 白做）。依赖按 GAV 坐标隔离、是只读
        #    下载物、不含项目源码，跨同语言项目复用反而加速，无泄漏风险，故保留。
        cache_dirs = ".cache .npm .cargo go/pkg .config/pip __pycache__ .pytest_cache .mypy_cache node_modules"
        cmd = (
            f"mkdir -p {workdir} && "
            f"find {workdir} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + && "
            f"find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + 2>/dev/null; "
            f'for d in {cache_dirs}; do rm -rf "$HOME/$d" 2>/dev/null; done; '
            f"echo WORKSPACE_CLEANED"
        )
        try:
            cr = self.run_command(sandbox, cmd, timeout=45, _skip_blacklist=True)
            ok = cr.success and "WORKSPACE_CLEANED" in (cr.stdout or "")
            if not ok:
                logger.warning("clean_workspace 未确认成功 %s: %s", sid, (cr.stdout or cr.error or "")[:200])
            return ok
        except Exception as exc:
            logger.warning("clean_workspace 失败 %s: %s", sid, exc)
            return False

    def _fail_threshold(self) -> int:
        return int(getattr(self.config, "sandbox_fail_threshold", 5) or 5)

    def _record_sandbox_failure(self, sid: str) -> None:
        """记录一次沙箱基础设施失败（5xx/连接类）。达阈值抛 SandboxUnhealthyError。

        仅由 run_code/run_command 的【基础设施错误】路径调用——命令真跑了但非0退出
        (编译失败等业务错误)不算，避免把"代码写错"误判成"沙箱坏"。
        """
        n = self._fail_counts.get(sid, 0) + 1
        self._fail_counts[sid] = n
        threshold = self._fail_threshold()
        if n >= threshold:
            logger.error(
                "沙箱 %s 连续 %d 次基础设施操作失败，触发熔断（疑似镜像/envd 故障）",
                sid, n,
            )
            raise SandboxUnhealthyError(
                f"沙箱 {sid} 连续 {n} 次操作失败（疑似镜像/envd 故障），已中止"
            )

    def _record_sandbox_success(self, sid: str) -> None:
        """一次成功操作 → 清零失败计数（连续失败才熔断，偶发抖动可恢复）。"""
        if self._fail_counts.get(sid):
            self._fail_counts[sid] = 0

    def health_check(self, sandbox: Any) -> bool:
        """envd 健康探活：跑一个轻量 shell 命令验证 envd shell 端点可用。

        借出/创建沙箱后调用；不健康（探活失败）则弃用换新沙箱。
        用 run_command（shell 端点，不依赖 Jupyter kernel）跑 echo 标记。

        关键：探活经 CubeProxy/openresty 网关，大量并发创建沙箱时网关会瞬时
        504 Gateway Time-out（实测一个任务因 504 雪崩弃用 17 个沙箱）。504/网关超时
        是【瞬时】错误，沙箱本身往往是好的——故对这类错误做短退避重试，区分"网关
        瞬时过载"与"沙箱真故障"，避免误弃用引发创建雪崩、加重网关负载、耗尽资源。
        """
        import time as _time
        sid = getattr(sandbox, "sandbox_id", None) or str(sandbox)
        _GATEWAY_HINTS = ("504", "gateway time-out", "gateway timeout",
                          "timeoutexception", "502", "bad gateway", "openresty")
        last_err = ""
        for _attempt in range(3):  # 最多 3 次（含首次），瞬时 504 通常第 2 次即恢复
            try:
                cr = self.run_command(sandbox, "echo __SWARM_HEALTH_OK__", timeout=15, _count_failures=False)
                ok = cr.success and "__SWARM_HEALTH_OK__" in (cr.stdout or "")
                if ok:
                    return True
                last_err = (cr.error or "")[:160]
            except Exception as exc:  # noqa: BLE001 — 探活不应让上层崩
                last_err = str(exc)[:160]
            # 仅对网关瞬时错误重试；非网关错误（真沙箱故障）立即判死不浪费时间
            if any(h in last_err.lower() for h in _GATEWAY_HINTS) and _attempt < 2:
                logger.info("沙箱 %s 探活遇网关瞬时错误(504/网关超时)，退避重试 [%d/3]: %s",
                            sid, _attempt + 1, last_err[:80])
                _time.sleep(2.0 * (_attempt + 1))  # 2s, 4s 退避
                continue
            break
        logger.warning("沙箱 %s 健康探活未通过: err=%s", sid, last_err)
        return False

    def run_command(self, sandbox: Any, command: str, timeout: int = 120, _count_failures: bool = True, _skip_blacklist: bool = False) -> "CodeResult":
        """在沙箱内执行 shell 命令 —— 走 SDK 原生 commands.run(shell 端点)。

        与 run_code 的区别：run_code 用 Jupyter kernel 端点(部分自建语言镜像未装
        kernel → 502)；commands.run 是 shell 端点，所有镜像都可用，且执行 mvn/
        npm/go 等构建命令更直接。优先用本方法跑 shell，run_code 仅用于真 Python 片段。

        _skip_blacklist: 内部系统命令（如 clean_workspace）跳过黑名单检查。
        """
        sid = getattr(sandbox, "sandbox_id", None) or str(sandbox)
        # A2 批3：命令安全黑名单（防误操作）。命中则拒绝执行 + 审计留痕。
        # db 不可用时 fail-open（check_command 内部处理），不阻断业务——真正安全边界是
        # CubeSandbox 沙箱隔离（已实测：非 root / 网络封锁 / 资源限额）。
        if not _skip_blacklist:
            # #1(b) fail-closed：用 hardened 入口——异常回退内置基线，绝不无条件放行
            # （旧 `except: allowed=True` 会把 import 失败/罕见异常下的 rm -rf / 也放行）。
            from swarm.config import command_blacklist_store
            allowed, reason = command_blacklist_store.check_command_hardened(command)
            if not allowed:
                logger.warning("[A2] 命令被黑名单拦截 sid=%s reason=%s cmd=%s", sid, reason, command[:120])
                try:
                    from swarm.audit import audit
                    audit("command_blocked", executor="Worker", sandbox_id=sid, reason=reason)
                except Exception:  # noqa: BLE001
                    pass
                self.append_activity(
                    sid, "exec",
                    f"命令被安全黑名单拦截（{reason}）— {command[:120]}",
                    code=command, error=f"blocked: {reason}",
                )
                return CodeResult(
                    stdout="", stderr=f"⛔ 命令被安全黑名单拦截：{reason}",
                    error=f"command_blocked: {reason}", success=False,
                )
        # 沙箱 python 镜像 PATH 只有 python3，无 python 别名。harness/trivial 生成的
        # "python -m py_compile" 等命令会 exit=127 command not found，导致 L1 误判失败
        # （task a4988789 实证）。在执行统一入口做幂等规范化：裸 python 调用 → python3。
        # 用词边界正则只替换独立 token，不误伤 pythonpath / python3 / /usr/bin/python。
        command = _normalize_python_cmd(command)
        logger.debug("Running command in sandbox %s: %s...", sid, command[:80])
        try:
            res = sandbox.commands.run(command, timeout=timeout)
            stdout = getattr(res, "stdout", "") or ""
            stderr = getattr(res, "stderr", "") or ""
            exit_code = getattr(res, "exit_code", 0)
            cr = CodeResult(
                stdout=stdout,
                stderr=stderr,
                error=None if exit_code == 0 else f"exit_code={exit_code}",
                success=(exit_code == 0),
            )
            self.append_activity(
                sid, "exec",
                f"run_command exit={exit_code} ({timeout}s) — {command[:120]}",
                stdout=stdout, stderr=stderr, code=command,
                error=cr.error,
            )
            self._record_sandbox_success(sid)
            return cr
        except Exception as exc:
            # commands.run 抛异常通常意味着非 0 退出码(SDK 行为)或连接问题。
            # CommandExitException 带 stdout/stderr/exit_code，尽量提取。
            stdout = getattr(exc, "stdout", "") or ""
            stderr = getattr(exc, "stderr", "") or ""
            exit_code = getattr(exc, "exit_code", None)
            if exit_code is not None:
                # 命令正常跑了但非 0 退出(如编译失败)——这是有效结果，非基础设施错误
                cr = CodeResult(stdout=stdout, stderr=stderr, error=f"exit_code={exit_code}", success=False)
                self.append_activity(sid, "exec", f"run_command exit={exit_code} — {command[:120]}",
                                     stdout=stdout, stderr=stderr, code=command, error=cr.error)
                self._record_sandbox_success(sid)  # envd 通了（命令真跑了），清零基础设施失败计数
                return cr
            err = f"{type(exc).__name__}: {exc}"
            logger.warning("Sandbox run_command failed for %s: %s", sid, str(exc)[:200])
            self.append_activity(sid, "exec", f"run_command 失败 — {err}", code=command, error=err)
            if _count_failures:
                # 基础设施错误（连接/5xx），计数+1，达阈值抛 SandboxUnhealthyError
                self._record_sandbox_failure(sid)
            return CodeResult(stdout=stdout, stderr=stderr, error=err, success=False)

    def run_code(self, sandbox: Any, code: str, timeout: int = 30) -> "CodeResult":
        """在沙箱中执行代码（捕获 SDK/代理异常，不向上抛 HTTP 500）"""
        sid = getattr(sandbox, "sandbox_id", None) or str(sandbox)
        logger.debug("Running code in sandbox %s: %s...", sid, code[:80])
        try:
            result = sandbox.run_code(code, timeout=timeout)
            cr = CodeResult.from_e2b(result)
            preview = code.strip().replace("\n", " ")[:120]
            self.append_activity(
                sid,
                "exec",
                f"run_code OK ({timeout}s) — {preview}",
                stdout=cr.stdout,
                stderr=cr.stderr,
                code=code,
                error=cr.error,
            )
            self._record_sandbox_success(sid)
            return cr
        except Exception as exc:
            logger.warning("Sandbox run_code failed for %s: %s", sid, exc)
            err = f"{type(exc).__name__}: {exc}"
            self.append_activity(
                sid,
                "exec",
                f"run_code 失败 — {err}",
                code=code,
                error=err,
            )
            # 注意：run_code 走 Jupyter kernel 端点，语言镜像(无 kernel)本就可能 502，
            # 属已知非致命情况，不计入熔断；熔断仅依赖 run_command(shell 端点，所有镜像通用)。
            return CodeResult(
                stdout="",
                stderr="",
                error=err,
                success=False,
            )

    def list_files(self, sandbox_id: str, path: str = "/") -> list[dict[str, Any]]:
        """列出沙箱内目录（优先 SDK files API，失败则 run_code fallback）"""
        sandbox = self._instances.get(sandbox_id)
        if sandbox is None:
            from e2b_code_interpreter import Sandbox as _Sandbox
            sandbox = _Sandbox.connect(sandbox_id)

        files: list[dict[str, Any]] = []
        try:
            if hasattr(sandbox, "files") and hasattr(sandbox.files, "list"):
                entries = sandbox.files.list(path)
                for ent in entries:
                    name = getattr(ent, "name", str(ent))
                    is_dir = getattr(ent, "is_dir", None)
                    if is_dir is None:
                        is_dir = getattr(ent, "type", "") == "dir"
                    abs_path = f"{path.rstrip('/')}/{name}"
                    if not abs_path.startswith("/"):
                        abs_path = "/" + abs_path
                    files.append({
                        "name": name,
                        "path": abs_path,
                        "is_dir": bool(is_dir),
                        "size": getattr(ent, "size", 0) or 0,
                    })
                return files
        except Exception as exc:
            logger.warning("sandbox.files.list failed for %s: %s", sandbox_id, exc)

        # 兜底：走 shell 端点(run_command)，不依赖 Jupyter kernel(语言镜像无 kernel→502)。
        # 用 ls -lAp 列目录：目录名带 / 后缀，便于判断 is_dir；解析每行 size。
        if hasattr(self, "run_command"):
            cr = self.run_command(
                sandbox,
                f"cd {path!r} 2>/dev/null && ls -lAp --time-style=+ 2>/dev/null || echo __LS_FAIL__",
                timeout=30,
            )
            out = (cr.stdout or "").strip()
            if out and out != "__LS_FAIL__":
                for line in out.splitlines():
                    line = line.rstrip()
                    if not line or line.startswith("total "):
                        continue
                    parts = line.split(None, 4)
                    if len(parts) < 5:
                        continue
                    perms, _links, _owner, size_s, name = parts
                    if name in (".", ".."):
                        continue
                    is_dir = perms.startswith("d") or name.endswith("/")
                    clean = name.rstrip("/")
                    try:
                        size = int(size_s) if not is_dir else 0
                    except ValueError:
                        size = 0
                    abs_path = f"{path.rstrip('/')}/{clean}"
                    if not abs_path.startswith("/"):
                        abs_path = "/" + abs_path
                    files.append({"name": clean, "path": abs_path, "is_dir": is_dir, "size": size})
                return files

        list_code = f"""
import os, json
path = {path!r}
items = []
for entry in sorted(os.scandir(path), key=lambda e: e.name):
    items.append({{"name": entry.name, "path": entry.path, "is_dir": entry.is_dir(), "size": entry.stat().st_size if entry.is_file() else 0}})
print(json.dumps(items))
"""
        result = self.run_code(sandbox, list_code, timeout=30)
        if result.error:
            raise RuntimeError(result.error)
        if result.stdout:
            import json as _json
            files = _json.loads(result.stdout.strip().split("\n")[-1])
        return files

    def sync_project_to_sandbox(
        self,
        sandbox: Any,
        local_root: Path,
        remote_root: str | None = None,
    ) -> dict[str, Any]:
        """将本地项目推送到沙箱 remote_root（默认 /workspace）。"""
        remote_root = remote_root or self.config.sandbox_remote_workdir
        stats: dict[str, Any] = {"uploaded": 0, "skipped": 0, "errors": []}
        local_root = Path(local_root).resolve()

        if not local_root.is_dir():
            stats["errors"].append(f"local_root is not a directory: {local_root}")
            logger.warning("Project sync skipped: %s", stats["errors"][-1])
            return stats

        use_files_api = hasattr(sandbox, "files") and hasattr(sandbox.files, "write")
        self._ensure_remote_dir(sandbox, remote_root, use_files_api)

        for path, rel, status in _iter_sync_candidates(local_root):
            if status == "large":
                stats["skipped"] += 1
                continue

            remote_path = f"{remote_root.rstrip('/')}/{rel.as_posix()}"
            try:
                data = path.read_bytes()
                if use_files_api:
                    sandbox.files.write(remote_path, data)
                else:
                    self._write_file_via_code(sandbox, remote_path, data)
                stats["uploaded"] += 1
            except Exception as exc:
                msg = f"{rel.as_posix()}: {exc}"
                stats["errors"].append(msg)
                logger.warning("Project sync file failed: %s", msg)

        logger.info(
            "Project sync to sandbox %s: uploaded=%d skipped=%d errors=%d",
            sandbox.sandbox_id,
            stats["uploaded"],
            stats["skipped"],
            len(stats["errors"]),
        )
        return stats

    def sync_sandbox_to_local(
        self,
        sandbox: Any,
        local_root: Path,
        remote_root: str | None = None,
    ) -> dict[str, Any]:
        """从沙箱 remote_root 拉取文件到本地镜像（产出阶段 pull-back）。"""
        remote_root = remote_root or self.config.sandbox_remote_workdir
        stats: dict[str, Any] = {"downloaded": 0, "skipped": 0, "errors": []}
        local_root = Path(local_root).resolve()

        def _should_skip_remote(rel_posix: str) -> bool:
            parts = rel_posix.split("/")
            if ".git" in parts:
                return True
            if any(part in EXCLUDED_DIRS for part in parts):
                return True
            if Path(rel_posix).suffix.lower() in EXCLUDED_EXTENSIONS:
                return True
            return False

        def _walk_remote_via_run_code() -> list[str]:
            import json as _json

            walk_code = f"""
import json, os
root = {remote_root!r}
skip_dirs = {{'.git', '__pycache__', '.venv', 'node_modules'}}
files = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in skip_dirs]
    for fn in filenames:
        files.append(os.path.join(dirpath, fn))
print(json.dumps(files))
"""
            result = self.run_code(sandbox, walk_code, timeout=120)
            if result.error or not result.stdout.strip():
                raise RuntimeError(result.error or result.stderr or "walk failed")
            line = result.stdout.strip().split("\n")[-1]
            return _json.loads(line)

        try:
            remote_files = _walk_remote_via_run_code()
        except Exception as exc:
            stats["errors"].append(f"walk {remote_root}: {exc}")
            remote_files = []

        for remote_path in remote_files:
            prefix = remote_root.rstrip("/")
            rel = remote_path[len(prefix) + 1 :] if remote_path.startswith(prefix + "/") else remote_path.lstrip("/")
            if _should_skip_remote(rel):
                stats["skipped"] += 1
                continue
            local_path = local_root / rel
            try:
                data = read_file_from_sandbox(sandbox, remote_path, manager=self)
                if isinstance(data, str):
                    data = data.encode("utf-8")
                if len(data) > MAX_SYNC_FILE_SIZE:
                    stats["skipped"] += 1
                    continue
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(data)
                stats["downloaded"] += 1
            except Exception as exc:
                msg = f"{rel}: {exc}"
                stats["errors"].append(msg)
                logger.warning("Sandbox pull-back file failed: %s", msg)

        logger.info(
            "Project sync from sandbox %s: downloaded=%d skipped=%d errors=%d",
            sandbox.sandbox_id,
            stats["downloaded"],
            stats["skipped"],
            len(stats["errors"]),
        )
        return stats

    def sync_files_to_sandbox(
        self,
        sandbox: Any,
        local_root: Path,
        rel_files: list[str],
        remote_root: str | None = None,
    ) -> dict[str, Any]:
        """精准上传：只把 rel_files 列出的文件推送到沙箱（不全量同步）。

        rel_files 为相对 local_root 的路径列表（来自子任务 scope）。
        缺失的本地文件记入 errors 但不中断其它文件上传。
        """
        remote_root = remote_root or self.config.sandbox_remote_workdir
        stats: dict[str, Any] = {"uploaded": 0, "skipped": 0, "errors": [], "files": []}
        local_root = Path(local_root).resolve()

        if not local_root.is_dir():
            stats["errors"].append(f"local_root is not a directory: {local_root}")
            logger.warning("Targeted sync skipped: %s", stats["errors"][-1])
            return stats

        use_files_api = hasattr(sandbox, "files") and hasattr(sandbox.files, "write")
        self._ensure_remote_dir(sandbox, remote_root, use_files_api)

        for rel in rel_files:
            rel_posix = Path(rel).as_posix().lstrip("/")
            if not rel_posix:
                continue
            local_path = (local_root / rel_posix).resolve()
            # 防目录穿越：必须在 local_root 内
            try:
                local_path.relative_to(local_root)
            except ValueError:
                stats["errors"].append(f"{rel_posix}: 越界路径，跳过")
                continue
            if not local_path.is_file():
                stats["errors"].append(f"{rel_posix}: 本地文件不存在")
                continue
            remote_path = f"{remote_root.rstrip('/')}/{rel_posix}"
            try:
                data = local_path.read_bytes()
                if use_files_api:
                    self._ensure_remote_dir(
                        sandbox,
                        remote_path.rsplit("/", 1)[0],
                        use_files_api,
                    )
                    sandbox.files.write(remote_path, data)
                else:
                    self._write_file_via_code(sandbox, remote_path, data)
                stats["uploaded"] += 1
                stats["files"].append(rel_posix)
                self._record_sandbox_success(sandbox.sandbox_id)
            except Exception as exc:
                stats["errors"].append(f"{rel_posix}: {exc}")
                logger.warning("Targeted upload failed: %s: %s", rel_posix, exc)
                # 上传走 envd 文件系统端点；5xx/连接错误计入熔断（这次 node 镜像故障即此类）。
                # 业务类错误(越界/本地文件不存在)在上面已 continue，不会到这里。
                self._record_sandbox_failure(sandbox.sandbox_id)

        logger.info(
            "Targeted sync to sandbox %s: uploaded=%d errors=%d files=%s",
            sandbox.sandbox_id,
            stats["uploaded"],
            len(stats["errors"]),
            stats["files"],
        )
        return stats

    def sync_files_from_sandbox(
        self,
        sandbox: Any,
        local_root: Path,
        rel_files: list[str],
        remote_root: str | None = None,
    ) -> dict[str, Any]:
        """精准拉回：只把 rel_files 列出的文件从沙箱拉回本地。

        返回 stats 含 contents={rel: text}，供 difflib 生成 diff。
        """
        remote_root = remote_root or self.config.sandbox_remote_workdir
        stats: dict[str, Any] = {
            "downloaded": 0, "skipped": 0, "errors": [], "contents": {}
        }
        local_root = Path(local_root).resolve()

        for rel in rel_files:
            rel_posix = Path(rel).as_posix().lstrip("/")
            if not rel_posix:
                continue
            remote_path = f"{remote_root.rstrip('/')}/{rel_posix}"
            try:
                data = read_file_from_sandbox(sandbox, remote_path, manager=self)
                if isinstance(data, str):
                    data = data.encode("utf-8")
                if len(data) > MAX_SYNC_FILE_SIZE:
                    stats["skipped"] += 1
                    continue
                local_path = (local_root / rel_posix).resolve()
                try:
                    local_path.relative_to(local_root)
                except ValueError:
                    stats["errors"].append(f"{rel_posix}: 越界路径，跳过")
                    continue
                # ── 行尾保留（task f20ea68d 根因·CRLF）──
                # RuoYi 等项目源文件是 Windows CRLF。worker 在沙箱用 patch/write 改文件后
                # 行尾变 LF，直接 write_bytes 覆盖会把本地 CRLF 文件【整体变成 LF】→ git HEAD
                # (CRLF) 与工作区(LF) 行尾不一致 → git diff 要么全文 churn，要么 --ignore-cr-at-eol
                # 产出 LF context 但 git apply 回不去 CRLF 的 HEAD（context 字节不匹配，
                # --ignore-whitespace/--3way 都救不了，因为差的是 CR 不是空白量）。
                # 修复：若【本地原文件】主体是 CRLF，则把沙箱返回的 LF 内容【转回 CRLF】再写，
                # 保持行尾与 git HEAD 一致 → git diff 同源、apply 必成功。二进制/已是 LF 的不动。
                data = self._preserve_line_endings(local_path, data)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                if _is_shared_manifest(rel_posix):
                    # 主干A：聚合清单写盘与 diff 用同一把 per-project flock 串行，杜绝并发 worker
                    # 在他人"重置自产出→diff"原子区内插入污染。锁不可用时退化为裸写（fail-open，
                    # 仅恢复旧争用风险，不阻塞）。非清单文件走 else 分支不加锁，保持并行无开销。
                    try:
                        from swarm.worker.executor import _ProjectGitFlock
                        with _ProjectGitFlock(local_root):
                            local_path.write_bytes(data)
                    except Exception:
                        local_path.write_bytes(data)
                else:
                    local_path.write_bytes(data)
                stats["downloaded"] += 1
                try:
                    stats["contents"][rel_posix] = data.decode("utf-8")
                except UnicodeDecodeError:
                    stats["contents"][rel_posix] = None  # 二进制
            except Exception as exc:
                stats["errors"].append(f"{rel_posix}: {exc}")
                logger.warning("Targeted pull-back failed: %s: %s", rel_posix, exc)

        logger.info(
            "Targeted sync from sandbox %s: downloaded=%d errors=%d",
            sandbox.sandbox_id,
            stats["downloaded"],
            len(stats["errors"]),
        )
        return stats

    @staticmethod
    def _preserve_line_endings(local_path: Path, new_data: bytes) -> bytes:
        """保持写回内容的行尾与本地原文件一致（task f20ea68d 根因·CRLF）。

        若本地原文件存在且主体为 CRLF（\\r\\n），而沙箱返回内容是 LF（worker 改写后），
        则把返回内容的裸 LF 转回 CRLF，使工作区行尾与 git HEAD 一致——这样 git diff
        产出的 context 行带正确 CRLF、git apply 同源必成功。

        判定与转换都保守：
          - 本地文件不存在（新建）→ 不转（按沙箱产出的行尾，通常 LF，新文件无 HEAD 约束）；
          - 本地或新内容含 NUL（二进制）→ 不动；
          - 本地主体不是 CRLF（已是 LF）→ 不动；
          - 转换用 \"先归一化到 LF 再统一替换为 CRLF\"，幂等，不会产生 \\r\\r\\n。
        """
        try:
            if not local_path.exists():
                return new_data
            old = local_path.read_bytes()
            # 二进制不处理
            if b"\x00" in old or b"\x00" in new_data:
                return new_data
            # 本地主体是否 CRLF：CRLF 行数占多数（容忍个别 LF）
            crlf = old.count(b"\r\n")
            lf_total = old.count(b"\n")
            if crlf == 0 or crlf < (lf_total - crlf):
                return new_data  # 本地不是 CRLF 主体 → 保持沙箱产出（LF）
            # 沙箱内容归一化到 LF 后统一转 CRLF（幂等：先 \r\n→\n 再 \n→\r\n）
            normalized = new_data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            return normalized.replace(b"\n", b"\r\n")
        except Exception:  # noqa: BLE001
            return new_data  # 任何异常都退回原始字节，不阻断 pull-back

    def _ensure_remote_dir(
        self, sandbox: Any, remote_root: str, use_files_api: bool
    ) -> None:
        if use_files_api and hasattr(sandbox.files, "make_dir"):
            try:
                sandbox.files.make_dir(remote_root)
                return
            except Exception as exc:
                logger.debug("files.make_dir failed for %s: %s", remote_root, exc)
        self.run_code(
            sandbox,
            f"import os; os.makedirs({remote_root!r}, exist_ok=True)",
            timeout=15,
        )

    def _write_file_via_code(
        self, sandbox: Any, remote_path: str, data: bytes
    ) -> None:
        """Fallback：base64 经 run_code 写入沙箱文件。"""
        encoded = base64.b64encode(data).decode("ascii")
        code = f"""
import base64, os
path = {remote_path!r}
os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
with open(path, 'wb') as f:
    f.write(base64.b64decode({encoded!r}))
print('OK')
"""
        result = self.run_code(sandbox, code, timeout=60)
        if not result.success or result.error:
            raise RuntimeError(result.error or result.stderr or "write via run_code failed")

    @property
    def active_count(self) -> int:
        return len(self._instances)

    @property
    def active_ids(self) -> list[str]:
        return list(self._instances.keys())


# ──────────────────────────────────────────────
# 代码执行结果
# ──────────────────────────────────────────────
class CodeResult(BaseModel):
    """统一的代码执行结果"""
    stdout: str = ""
    stderr: str = ""
    text: str = ""
    error: str | None = None
    success: bool = True

    @classmethod
    def from_e2b(cls, result: Any) -> "CodeResult":
        """从 E2B Execution 结果转换"""
        stdout = ""
        stderr = ""
        error_str = None
        success = True

        if hasattr(result, "logs") and result.logs:
            stdout = "".join(result.logs.stdout) if result.logs.stdout else ""
            stderr = "".join(result.logs.stderr) if result.logs.stderr else ""

        if hasattr(result, "error") and result.error:
            error_str = str(result.error)
            success = False

        text = ""
        if hasattr(result, "text") and result.text is not None:
            text = str(result.text)

        return cls(
            stdout=stdout.strip(),
            stderr=stderr.strip(),
            text=text.strip(),
            error=error_str,
            success=success,
        )


# ──────────────────────────────────────────────
# 旧 SandboxPool（已移除，P2 死桩清理）
# ──────────────────────────────────────────────
# 原 SandboxPool 是失效死代码：dispatch 每次 new 一个临时实例、warmup 把沙箱塞进它的
# _pool 后实例即被 GC，预热指针随之丢失（见 brain/nodes/dispatch.py 的历史注释）。
# 真正生效的是单例 HotSandboxPool（worker/sandbox_pool.py）。本类已删除，勿再引用。
