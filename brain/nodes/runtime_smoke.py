"""S1-3（task#17）：沙箱运行时冒烟探针执行器。

设计定案见 docs/RUNTIME_SMOKE_DESIGN.md §1.4/§2/§6 与"给 task#17 的实现指引"。

主循环裁决（不赌未证实的"后台进程跨 run_command 存活"能力）：
- 探针主形态 = **单次 run_command 内自包含 bash 脚本**：脚本内起后台进程、
  轮询探活、tail 日志、finally（trap EXIT）必杀进程组，以结构化标记输出结果。
  该形态在"跨调用存活/不存活"两种未知结论下都成立；跨调用形态不做。
- 执行通道唯一 = `manager.run_command`（worker/sandbox.py:919，shell 端点）；
  **禁 run_code**（Jupyter kernel 端点，自建语言镜像 502）。
- infra 失败 ≠ 冒烟失败：run_command 异常 / `__SMOKE_DONE__` 标记缺失 / envd 5xx
  → 返"未执行"（skipped），对齐 D31 `_run_reactor_build_in_sandbox` 的 ran/ok
  区分（brain/nodes/__init__.py:1997-2004）与 `__RC__` 标记口径。

三分类（§6.2 形态学，栈无关设计原则）：
- 分类器本体只吃 (exit_code/进程存活, 日志文本, 探活序列) 通用三元组；
- 栈词汇**只**出现在按语言 keyed 的正则数据表（本文件顶部常量）；
- 类1 代码错误（import/类加载/模块解析/语法失败族）→ failed（可回灌）；
- 类2 外部依赖缺失/端口占用（环境）→ skipped（不冤枉代码）；
- 类3 超时/无形态命中 → skipped + degraded（**绝不默认判代码错**）；
- 两族双命中 = 歧义 → 保守 skipped + WARN 留痕。

本模块只提供可被 task#18（graph/state 接线）调用的函数，不碰 graph/state。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── 超时预算：env SWARM_RUNTIME_SMOKE_TIMEOUT_SEC，默认 180s，非法值回退默认 ──
DEFAULT_SMOKE_TIMEOUT_SEC = 180
# ── prepare 预算（F1）：env SWARM_RUNTIME_SMOKE_PREPARE_TIMEOUT_SEC，默认 600s ──
# prepare = 起应用前的构建产物命令（mvn package 等）；JVM 冷 package 可到数分钟。
DEFAULT_PREPARE_TIMEOUT_SEC = 600
# run_command 的 timeout = 探活窗口 + 缓冲（脚本自身收尾：kill/tail/标记输出）
RUN_TIMEOUT_BUFFER_SEC = 90
# 日志尾部收割限行
DEFAULT_LOG_TAIL_LINES = 200
# 探活轮询间隔（秒）
PROBE_INTERVAL_SEC = 2

# ── 结构化输出标记（与 L2 的 __RC__ 口径同族） ──
MARK_PHASE = "__SMOKE_PHASE__"
MARK_PROBE_TOOL = "__SMOKE_PROBE_TOOL__"
MARK_PROBE = "__SMOKE_PROBE__"                 # ok|refused|timeout|exited
MARK_APP_RC = "__SMOKE_APP_RC__"               # alive|<int>
MARK_PREPARE_RC = "__SMOKE_PREPARE_RC__"       # F1：prepare 命令退出码
MARK_PORT_BUSY = "__SMOKE_PORT_BUSY__"         # F4：起应用前端口已有 listener → 环境
MARK_LOG_BEGIN = "__SMOKE_LOG_TAIL_BEGIN__"
MARK_LOG_END = "__SMOKE_LOG_TAIL_END__"
MARK_DONE = "__SMOKE_DONE__"
MARK_PROBE_TOOL_MISSING = "PROBE_TOOL_MISSING"  # 环境缺探活工具 → 上层判 skipped
# S2-5：assert 段执行工具缺失标记（断言片段是 curl 形态——acceptance_spec 契约；curl 缺失
# 时脚本如实输出本标记，phase 侧判 skipped:assert_tool_missing，环境缺失绝不伪装断言失败）
MARK_ACCEPT_TOOL_MISSING = "__ACCEPT_TOOL_MISSING__"
# S2-5：accept 标记行透传令牌——executor 只按此令牌原样透传断言证据行（不解析；解析由
# acceptance_spec.parse_probe_output 在 verify_runtime accept phase 侧做）
ACCEPT_MARK_TOKEN = "__ACCEPT_"


# ═══════════════ 三分类正则数据表（仅此处含栈词汇，按语言 keyed） ═══════════════
# 语言键归一：project_stack.backend 形如 "Spring Boot (java)" / "java"。
# JVM 族（kotlin/scala）类加载失败形态与 java 同源，归并到 java 表。
# 顺序敏感：先长后短，避免 "java" 吞 "javascript"（\b 已防，但保持显式序）。
_LANGUAGE_ALIASES: tuple[tuple[str, str], ...] = (
    ("javascript/typescript", "node"),
    ("typescript", "node"),
    ("javascript", "node"),
    ("node", "node"),
    ("kotlin", "java"),
    ("scala", "java"),
    ("java", "java"),
    ("python", "python"),
    ("golang", "go"),
    ("go", "go"),
    ("rust", "rust"),
)

# 类1：代码错误族（纯语法/类格式/链接失败——与依赖安装状态无关的确定性代码故障）
# → failed，可回灌。注意：**没有 generic 条目**——无表命中绝不默认判代码错（类3 兜底）。
#
# F5 裁决（推翻设计文档 §6.3 的"panic 默认类1"赌注）：go/rust 的裸 `panic:` 不再默认判
# 代码错——沙箱内无外部服务/无环境变量，配置缺失 panic（required key missing 等）是高频
# 环境形态，默认类1 会把环境冤枉成代码。改为：只有命中【代码故障形态子表】（nil pointer/
# index out of range/unwrap on Err|None…）才判类1；环境形态入类2 表；裸 panic 无子形态
# 命中 → 类3 inconclusive skipped（绝不默认判代码错）。
_CODE_ERROR_PATTERNS: dict[str, tuple[str, ...]] = {
    "java": (
        r"UnsupportedClassVersionError",
        r"ClassFormatError",
        r"java\.lang\.NoSuchMethodError",
        # R67-hunter(a)：Spring 容器【纯代码性】启动崩溃——路由双实现/bean 名冲突（③b/③c
        # 规划期闸的运行期兜底，round67 /notify 双 Controller 死型）。此前不在任何模式族 →
        # 被吞成 skipped/inconclusive=闸门承诺的兜底不存在。刻意【不加】BeanCreationException/
        # APPLICATION FAILED TO START——它们常裹外部依赖缺失（DB 连不上），会把环境性 skip
        # 冤判 code_error（fail-honest：环境绝不伪装代码失败）。
        r"Ambiguous mapping",
        r"ConflictingBeanDefinitionException",
    ),
    "node": (
        r"\bSyntaxError\b",
        r"\bReferenceError\b",
    ),
    "python": (
        r"\bSyntaxError\b",
        r"\bIndentationError\b",
    ),
    "go": (
        r"panic: runtime error",
        r"nil pointer dereference",
        r"index out of range",
        r"slice bounds out of range",
    ),
    "rust": (
        r"index out of bounds",
        r"unwrap\(\)`? on (an? )?(`?Err`?|`?None`?)",
        r"attempt to .{0,40}overflow",
    ),
}

# F2：import/模块/类加载缺失族——捕获组提取缺失符号名（模块名/类 FQN，按语言表）。
# 判定不再硬归类1：符号解析为【项目内】→ code_error failed；【项目外/无法解析】→
# dependency_missing skipped（沙箱不保证装项目第三方运行时依赖，环境绝不伪装代码失败）。
# 无捕获组的兜底条目（裸 ERR_MODULE_NOT_FOUND 等）= 命中但无符号 → 按无法解析处理。
_IMPORT_MISSING_PATTERNS: dict[str, tuple[str, ...]] = {
    "java": (
        r"ClassNotFoundException:?\s+([\w.$]+)",
        r"NoClassDefFoundError:?\s+([\w/.$]+)",
        r"ClassNotFoundException",
        r"NoClassDefFoundError",
    ),
    "node": (
        r"Cannot find module '([^']+)'",
        r"Cannot find package '([^']+)'",
        r"Cannot find module",
        r"ERR_MODULE_NOT_FOUND",
    ),
    "python": (
        r"No module named '([^']+)'",
        r"ImportError: cannot import name '[^']+' from '([^']+)'",
        r"\bModuleNotFoundError\b",
        r"\bImportError\b",
    ),
}

# 类2：外部依赖缺失/环境族（对外连接拒绝/鉴权失败/端口自占用）→ skipped。
# 匹配用 IGNORECASE（同一形态在各驱动/OS 上大小写漂移）。
_ENV_MISSING_PATTERNS: dict[str, tuple[str, ...]] = {
    "generic": (
        r"Connection refused",
        r"ECONNREFUSED",
        r"EADDRINUSE",
        r"Address already in use",
        r"password authentication failed",
        r"Access denied for user",
        r"Name or service not known",
        r"getaddrinfo ENOTFOUND",
    ),
    "java": (
        r"Port \d+ was already in use",
        r"Communications link failure",
        r"Unable to acquire JDBC Connection",
    ),
    "node": (),
    "python": (
        r"OperationalError",
    ),
    "go": (
        r"dial tcp .*connection refused",
        r"bind: address already in use",
        # F5：panic 环境形态（配置/文件缺失——沙箱内无外部服务/env 是常态）
        r"required key .{0,60} missing",
        r"missing .{0,60}(env|environment) variable",
        r"no such file or directory",
    ),
    "rust": (
        r"Connection refused \(os error 111\)",
        # F5：panic 环境形态（同上）
        r"required key .{0,60} missing",
        r"missing .{0,60}(env|environment) variable",
        r"No such file or directory \(os error 2\)",
        r"environment variable not found",
    ),
}

# bind 成功族：进程自报已监听（用于"进程活+日志 bind 成功+TCP 不通"= 沙箱网络
# 异常 → skipped 不判 failed）。匹配用 IGNORECASE。
_BIND_SUCCESS_PATTERNS: dict[str, tuple[str, ...]] = {
    "generic": (
        r"listening on",
        r"listening at",
        r"server (is )?(started|running)",
        r"running on https?://",
        r"started server on",
    ),
    "java": (
        r"Tomcat started on port",
        r"Netty started on port",
        r"Jetty started on port",
        r"Started \S+ in [\d.]+ seconds",
    ),
    "node": (),
    "python": (
        r"Uvicorn running on",
        r"Development server at",
    ),
    "go": (),
    "rust": (),
}


# ═══════════════════════════ 结果对象 ═══════════════════════════

@dataclass
class RuntimeSmokeResult:
    """三态冒烟结果。status ∈ passed|failed|skipped。

    classification 词表：
      started（passed）/ code_error（failed）/
      env_missing | dependency_missing | inconclusive | ambiguous | network_anomaly |
      stale_listener_suspected | port_busy | prepare_failed |
      probe_tool_missing | not_executed（均 skipped）。
    """
    status: str
    classification: str
    message: str
    log_tail: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════ 纯函数层 ═══════════════════════════

def _resolve_positive_int_env(env_name: str, default: int) -> int:
    """正整数 env 解析：缺失/非法/非正 → 默认值（回退必留 WARN 可观测）。"""
    raw = (os.environ.get(env_name, "") or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("[RUNTIME_SMOKE] %s 非法值 %r，回退默认 %ds", env_name, raw, default)
        return default
    if val <= 0:
        logger.warning("[RUNTIME_SMOKE] %s 非正值 %r，回退默认 %ds", env_name, raw, default)
        return default
    return val


def resolve_smoke_timeout_sec() -> int:
    """探活窗口：env SWARM_RUNTIME_SMOKE_TIMEOUT_SEC；非法/非正 → 默认 180。"""
    return _resolve_positive_int_env("SWARM_RUNTIME_SMOKE_TIMEOUT_SEC", DEFAULT_SMOKE_TIMEOUT_SEC)


def resolve_prepare_timeout_sec() -> int:
    """prepare 预算（F1）：env SWARM_RUNTIME_SMOKE_PREPARE_TIMEOUT_SEC；非法/非正 → 默认 600。"""
    return _resolve_positive_int_env(
        "SWARM_RUNTIME_SMOKE_PREPARE_TIMEOUT_SEC", DEFAULT_PREPARE_TIMEOUT_SEC)


# ═══════════ F2：项目内符号索引（import 缺失归属判定的证据面） ═══════════

_PROJECT_SYMBOLS_MAX_DIRS = 3000
_PROJECT_SYMBOLS_MAX_FILES = 50_000
# 源文件扩展名族（按语言 keyed，import 符号路径化后的落盘形态）
_SOURCE_EXTS: dict[str, tuple[str, ...]] = {
    "java": (".java", ".kt", ".kts", ".scala"),
    "node": (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"),
    "python": (".py",),
}


def build_project_symbols(project_path: str) -> dict[str, Any]:
    """有界 os.walk 建项目内符号索引（纯函数，只读文件 IO，绝不抛）。

    产出 {"paths": 源文件 relpath 集(posix), "basenames": 文件名集, "top": 顶层名集
    （顶层目录名 + 顶层文件 stem）}——供 _symbol_is_project_internal 判定
    "缺失符号是否项目自身模块"。
    """
    paths: set[str] = set()
    basenames: set[str] = set()
    top: set[str] = set()
    try:
        from swarm.brain.stack_detect import _NOISE_DIRS
        root_str = str(project_path or "")
        dir_count = 0
        for root, dirs, files in os.walk(root_str):
            dirs[:] = sorted(d for d in dirs if d not in _NOISE_DIRS)
            dir_count += 1
            if dir_count > _PROJECT_SYMBOLS_MAX_DIRS or len(paths) > _PROJECT_SYMBOLS_MAX_FILES:
                break
            rel = os.path.relpath(root, root_str)
            rel = "" if rel == "." else rel.replace(os.sep, "/")
            if not rel:
                top.update(dirs)
            for f in files:
                paths.add(f"{rel}/{f}" if rel else f)
                basenames.add(f)
                if not rel:
                    top.add(os.path.splitext(f)[0])
    except Exception:  # noqa: BLE001 — 索引建不出=空索引，下游按无法解析保守处理
        pass
    return {"paths": paths, "basenames": basenames, "top": top}


def _symbol_is_project_internal(symbol: str, language_key: str | None,
                                project_symbols: dict[str, Any] | None) -> bool | None:
    """缺失符号 → 项目内(True)/项目外(False)/无法解析(None)。

    无 project_symbols → None（上层保守 dependency_missing，不冤枉代码）。
    """
    if not symbol or not isinstance(project_symbols, dict):
        return None
    paths = project_symbols.get("paths") or set()
    basenames = project_symbols.get("basenames") or set()
    top = project_symbols.get("top") or set()
    try:
        if language_key == "java":
            # 类 FQN → 路径化（内部类剥 $），前缀命中项目源码路径（尾段边界匹配）
            fqn = symbol.replace("/", ".").split("$")[0].strip(".")
            if "." not in fqn:
                return None  # 无包名的裸类名不足以解析归属
            pathified = fqn.replace(".", "/")
            for p in paths:
                stem, ext = os.path.splitext(p)
                if ext not in _SOURCE_EXTS["java"]:
                    continue
                if stem == pathified or stem.endswith("/" + pathified):
                    return True
            return False
        if language_key == "python":
            first = symbol.split(".")[0].strip()
            if not first:
                return None
            return (f"{first}.py" in basenames) or (first in top)
        if language_key == "node":
            s = symbol.strip()
            if s.startswith((".", "/")):
                return True  # 相对/绝对路径 import = 项目自身文件缺失 → 代码错
            name = s.split("/")[0]
            if not name or name.startswith("@"):
                return False  # scoped 包必为第三方
            if name in top:
                return True
            return any(f"{name}{ext}" in basenames for ext in _SOURCE_EXTS["node"])
        return None
    except Exception:  # noqa: BLE001 — 解析异常=无法解析（保守）
        return None


def normalize_language_key(backend: str | None) -> str | None:
    """project_stack.backend（如 "Spring Boot (java)"）→ 数据表语言键。

    词边界匹配防误配（'django' 不得命中 'go'）；无法归一 → None（只走 generic 表）。
    """
    if not backend:
        return None
    text = str(backend).lower()
    for token, key in _LANGUAGE_ALIASES:
        if "/" in token:
            if token in text:
                return key
        elif re.search(rf"\b{re.escape(token)}\b", text):
            return key
    return None


def build_smoke_script(
    start_cmd: str,
    port: int | str,
    health_path: str = "/",
    *,
    prepare_cmd: str | None = None,
    timeout_sec: int | None = None,
    workdir: str = "/workspace",
    log_tail_lines: int = DEFAULT_LOG_TAIL_LINES,
    assert_cmds: list[str] | None = None,
) -> str:
    """生成自包含 bash 冒烟脚本（纯函数，可单测）。

    形态：单次 run_command 内 —— [F4 端口预检（已有 listener → PORT_BUSY 提前退出，
    不起应用）] → [F1 prepare（构建产物命令，独立日志；非 0 → PREPARE_RC + prepare
    日志尾 + 完整收尾标记后提前退出，不起应用）] → 起后台(记 PID) → 轮询探活(2s)
    → [S2-5 assert 段（仅探活 ok 才执行）] → 收割日志尾部 → trap EXIT 必杀进程组
    （kill -- -PID + pkill -P 兜底）→ 结构化标记输出。

    探活工具运行时自适应：curl → bash /dev/tcp → python3 socket → PROBE_TOOL_MISSING
    （环境缺失绝不伪装代码失败，上层判 skipped）。

    assert_cmds（S2-5）：验收断言自包含 curl 片段列表（acceptance_spec.assertion_to_probe_cmd
    产出，含 __ACCEPT_* 标记输出）。插在探活 ok 之后、收割/必杀之前——应用确认活着断言才有
    意义；探活未 ok（timeout/exited）则整段跳过（断言证据缺失由 phase 侧按跟随 skip 处理）。
    单条失败不提前退出（跑完收全证据）；curl 缺失如实输出 MARK_ACCEPT_TOOL_MISSING。
    缺省 None/空 → 生成脚本与既有行为逐字节一致。
    """
    window = timeout_sec if (isinstance(timeout_sec, int) and timeout_sec > 0) \
        else resolve_smoke_timeout_sec()
    port_num = int(port)
    hp = str(health_path or "/")
    if not hp.startswith("/"):
        hp = "/" + hp
    q_cmd = shlex.quote(start_cmd)
    q_workdir = shlex.quote(workdir)
    q_health = shlex.quote(hp)
    tail_n = int(log_tail_lines)
    prepare_block = ""
    if prepare_cmd and str(prepare_cmd).strip():
        q_prepare = shlex.quote(str(prepare_cmd))
        prepare_block = f"""SMOKE_PREPARE_CMD={q_prepare}
SMOKE_PREPARE_LOG=".swarm_smoke_prepare.log"
: > "$SMOKE_PREPARE_LOG"
echo "{MARK_PHASE}prepare"
bash -c "$SMOKE_PREPARE_CMD" >"$SMOKE_PREPARE_LOG" 2>&1
SMOKE_PREPARE_RC=$?
echo "{MARK_PREPARE_RC}$SMOKE_PREPARE_RC"
if [ "$SMOKE_PREPARE_RC" != "0" ]; then
  echo "{MARK_PHASE}collect"
  echo "{MARK_LOG_BEGIN}"
  tail -n {tail_n} "$SMOKE_PREPARE_LOG" 2>/dev/null
  echo "{MARK_LOG_END}"
  echo "{MARK_DONE}"
  exit 0
fi
"""
    accept_block = ""
    accept_cmds = [str(c) for c in (assert_cmds or []) if str(c).strip()]
    if accept_cmds:
        joined_asserts = "\n".join(accept_cmds)
        accept_block = f"""if [ "$SMOKE_OK" = "1" ]; then
  echo "{MARK_PHASE}accept"
  if command -v curl >/dev/null 2>&1; then
{joined_asserts}
  else
    echo "{MARK_ACCEPT_TOOL_MISSING}"
  fi
fi
"""
    return f"""set +e
SMOKE_START_CMD={q_cmd}
SMOKE_PORT={port_num}
SMOKE_HEALTH={q_health}
SMOKE_TIMEOUT={window}
SMOKE_INTERVAL={PROBE_INTERVAL_SEC}
cd {q_workdir} || {{ echo "{MARK_PHASE}workdir_unavailable"; exit 96; }}
SMOKE_LOG=".swarm_smoke_app.log"
: > "$SMOKE_LOG"
PROBE_TOOL=""
if command -v curl >/dev/null 2>&1; then
  PROBE_TOOL="curl"
elif [ -n "$BASH_VERSION" ]; then
  PROBE_TOOL="devtcp"
elif command -v python3 >/dev/null 2>&1; then
  PROBE_TOOL="python3"
fi
if [ -z "$PROBE_TOOL" ]; then
  echo "{MARK_PROBE_TOOL_MISSING}"
  echo "{MARK_PROBE_TOOL}MISSING"
else
  echo "{MARK_PROBE_TOOL}$PROBE_TOOL"
fi
smoke_probe_once() {{
  case "$PROBE_TOOL" in
    curl) curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${{SMOKE_PORT}}${{SMOKE_HEALTH}}" ;;
    devtcp) (exec 3<>"/dev/tcp/127.0.0.1/${{SMOKE_PORT}}") 2>/dev/null ;;
    python3) python3 -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',${{SMOKE_PORT}}))==0 else 1)" ;;
    *) return 1 ;;
  esac
}}
if [ -n "$PROBE_TOOL" ] && smoke_probe_once; then
  echo "{MARK_PORT_BUSY}"
  echo "{MARK_PHASE}collect"
  echo "{MARK_LOG_BEGIN}"
  echo "{MARK_LOG_END}"
  echo "{MARK_DONE}"
  exit 0
fi
{prepare_block}echo "{MARK_PHASE}start"
if command -v setsid >/dev/null 2>&1; then
  setsid bash -c "$SMOKE_START_CMD" >"$SMOKE_LOG" 2>&1 &
else
  bash -c "$SMOKE_START_CMD" >"$SMOKE_LOG" 2>&1 &
fi
SMOKE_PID=$!
smoke_cleanup() {{
  kill -- -"$SMOKE_PID" 2>/dev/null
  kill "$SMOKE_PID" 2>/dev/null
  pkill -P "$SMOKE_PID" 2>/dev/null
  return 0
}}
trap smoke_cleanup EXIT INT TERM
echo "{MARK_PHASE}probe"
SMOKE_OK=0
if [ -n "$PROBE_TOOL" ]; then
  SMOKE_DEADLINE=$(( $(date +%s) + SMOKE_TIMEOUT ))
  while [ "$(date +%s)" -lt "$SMOKE_DEADLINE" ]; do
    if ! kill -0 "$SMOKE_PID" 2>/dev/null; then
      echo "{MARK_PROBE}exited"
      break
    fi
    if smoke_probe_once; then
      echo "{MARK_PROBE}ok"
      SMOKE_OK=1
      break
    fi
    echo "{MARK_PROBE}refused"
    sleep "$SMOKE_INTERVAL"
  done
  if [ "$SMOKE_OK" != "1" ] && kill -0 "$SMOKE_PID" 2>/dev/null; then
    echo "{MARK_PROBE}timeout"
  fi
fi
{accept_block}echo "{MARK_PHASE}collect"
if kill -0 "$SMOKE_PID" 2>/dev/null; then
  echo "{MARK_APP_RC}alive"
else
  wait "$SMOKE_PID"
  echo "{MARK_APP_RC}$?"
fi
echo "{MARK_LOG_BEGIN}"
tail -n {tail_n} "$SMOKE_LOG" 2>/dev/null
echo "{MARK_LOG_END}"
echo "{MARK_DONE}"
exit 0"""


def parse_smoke_markers(output: str) -> dict[str, Any]:
    """从沙箱 stdout+stderr 解析结构化标记（纯函数）。"""
    out = output or ""
    probe_sequence = re.findall(rf"{MARK_PROBE}(\w+)", out)
    tool_m = re.search(rf"{MARK_PROBE_TOOL}(\w+)", out)
    probe_tool = tool_m.group(1) if tool_m else None
    rc_m = re.search(rf"{MARK_APP_RC}(alive|-?\d+)", out)
    app_rc: str | int | None
    if rc_m is None:
        app_rc = None
    elif rc_m.group(1) == "alive":
        app_rc = "alive"
    else:
        app_rc = int(rc_m.group(1))
    b = out.find(MARK_LOG_BEGIN)
    e = out.find(MARK_LOG_END)
    log_tail = out[b + len(MARK_LOG_BEGIN):e].strip("\n") if (b != -1 and e > b) else ""
    prep_m = re.search(rf"{MARK_PREPARE_RC}(-?\d+)", out)
    return {
        "probe_sequence": probe_sequence,
        "probe_tool": probe_tool,
        "probe_tool_missing": (MARK_PROBE_TOOL_MISSING in out) or (probe_tool == "MISSING"),
        "app_rc": app_rc,
        "prepare_rc": int(prep_m.group(1)) if prep_m else None,   # F1
        "port_busy": MARK_PORT_BUSY in out,                        # F4
        "log_tail": log_tail,
        "phases": re.findall(rf"{MARK_PHASE}(\w+)", out),
        "done": MARK_DONE in out,
    }


def _match_family(table: dict[str, tuple[str, ...]], language_key: str | None,
                  text: str, *, flags: int = 0) -> list[str]:
    patterns: list[str] = list(table.get("generic", ()))
    if language_key:
        patterns.extend(table.get(language_key, ()))
    return [p for p in patterns if re.search(p, text, flags)]


def _match_import_missing(language_key: str | None, text: str) -> list[tuple[str, str | None]]:
    """import 缺失族匹配（F2）→ [(pattern, 捕获符号|None)]。同一符号去重，保序。"""
    out: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for p in _IMPORT_MISSING_PATTERNS.get(language_key or "", ()):
        m = re.search(p, text)
        if not m:
            continue
        symbol = m.group(1) if m.re.groups else None
        key = (p, symbol)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def classify_smoke_outcome(
    app_rc: str | int | None,
    log_tail: str,
    probe_sequence: list[str],
    *,
    language_key: str | None = None,
    project_symbols: dict[str, Any] | None = None,
    probe_port: int | None = None,
) -> RuntimeSmokeResult:
    """三分类器（纯函数）：只吃 (进程侧, 日志文本, 探活序列) 通用三元组
    + 可选证据面（project_symbols=项目内符号索引，probe_port=探测端口留痕）。

    优先级：探活 ok 且进程存活 → passed（F4：ok 但进程已退 → stale_listener_suspected
    skipped，探活应答者身份存疑不假绿）；import 缺失族按符号归属裁决（F2：项目内 →
    类1，项目外/无法解析 → dependency_missing skipped）；类1/类2 双命中 → 歧义 skipped；
    类1 → failed；类2 → skipped；进程活+bind 成功+TCP 不通 → 网络异常 skipped
    （F6：附端口错配提示）；其余（超时/无形态） → inconclusive skipped + degraded。
    """
    log_text = log_tail or ""
    details: dict[str, Any] = {
        "app_rc": app_rc,
        "probe_sequence": list(probe_sequence),
        "language_key": language_key,
    }
    if "ok" in probe_sequence:
        if app_rc == "alive":
            return RuntimeSmokeResult(
                "passed", "started", "运行时冒烟通过：应用启动且探活应答",
                log_tail=log_text, details=details)
        # F4：探活曾 ok 但被测进程已退出——应答者可能是残留 listener/其他进程，不假绿
        logger.warning(
            "[RUNTIME_SMOKE] 探活 ok 但应用进程已退出(app_rc=%s)——应答者身份存疑，"
            "保守 skipped 不判 passed", app_rc)
        return RuntimeSmokeResult(
            "skipped", "stale_listener_suspected",
            "探活曾应答但应用进程已退出，应答者身份存疑（疑残留 listener），不判通过也不冤枉代码",
            log_tail=log_text, details=details)

    code_hits = _match_family(_CODE_ERROR_PATTERNS, language_key, log_text)
    env_hits = _match_family(_ENV_MISSING_PATTERNS, language_key, log_text,
                             flags=re.IGNORECASE)
    # F2：import 缺失族按符号归属裁决——项目内符号缺失=代码错；项目外/解析不出=依赖缺失（环境）
    import_hits = _match_import_missing(language_key, log_text)
    internal_import_hits: list[str] = []
    external_import_hits: list[tuple[str, str | None]] = []
    for pattern, symbol in import_hits:
        verdict = _symbol_is_project_internal(symbol, language_key, project_symbols) \
            if symbol else None
        if verdict is True:
            internal_import_hits.append(pattern)
        else:
            external_import_hits.append((pattern, symbol))
    code_hits = code_hits + internal_import_hits
    details["code_error_hits"] = code_hits
    details["env_missing_hits"] = env_hits
    if import_hits:
        details["import_missing_hits"] = [
            {"pattern": p, "symbol": s} for p, s in import_hits]

    if code_hits and env_hits:
        logger.warning(
            "[RUNTIME_SMOKE] 三分类歧义（代码错误族+外部依赖族双命中），保守判不可判 skipped："
            "code=%s env=%s", code_hits, env_hits)
        return RuntimeSmokeResult(
            "skipped", "ambiguous",
            "启动日志同时命中代码错误族与外部依赖族，不可判定，保守跳过（不冤枉代码）",
            log_tail=log_text, details=details)
    if code_hits:
        return RuntimeSmokeResult(
            "failed", "code_error",
            f"启动失败：命中代码错误形态 {code_hits[:3]}（可回灌修复）",
            log_tail=log_text, details=details)
    if env_hits:
        return RuntimeSmokeResult(
            "skipped", "env_missing",
            f"外部依赖/环境缺失形态 {env_hits[:3]}：沙箱内无外部服务，不判代码失败",
            log_tail=log_text, details=details)
    if external_import_hits:
        # F2：缺失符号是项目外（第三方）或解析不出——沙箱不保证装项目运行时依赖，
        # 环境缺失绝不伪装代码失败（skipped 必可观测：WARN + details 留符号）
        missing_symbols = [s for _, s in external_import_hits if s]
        logger.warning(
            "[RUNTIME_SMOKE] import/模块缺失但符号非项目内(或无法解析)，判依赖缺失(环境)"
            " skipped：symbols=%s（沙箱未装第三方运行时依赖是常态，不冤枉代码）",
            missing_symbols[:5])
        return RuntimeSmokeResult(
            "skipped", "dependency_missing",
            f"运行时依赖缺失（项目外符号 {missing_symbols[:3] or '无法解析'}）：沙箱不保证安装"
            "项目第三方运行时依赖，环境缺失不判代码失败",
            log_tail=log_text, details=details)

    bind_hits = _match_family(_BIND_SUCCESS_PATTERNS, language_key, log_text,
                              flags=re.IGNORECASE)
    if app_rc == "alive" and bind_hits:
        details["bind_success_hits"] = bind_hits
        if probe_port is not None:
            details["probe_port"] = probe_port
        # F6：日志自报监听成功但推导端口探不通——可能是端口推导错配（应用实际监听
        # 别的端口）。classification 不变（skip 方向已安全），仅补提示+端口值留痕。
        port_hint = f"（探测端口={probe_port}，日志自报监听端口可能与之不同——" \
                    "可能为端口推导错配）" if probe_port is not None else \
                    "（可能为端口推导错配：应用实际监听端口或与推导端口不同）"
        return RuntimeSmokeResult(
            "skipped", "network_anomaly",
            f"进程存活且日志显示监听成功但探活不通，疑沙箱网络异常{port_hint}，不判代码失败",
            log_tail=log_text, details=details)

    details["degraded"] = True
    return RuntimeSmokeResult(
        "skipped", "inconclusive",
        "探活窗口耗尽/进程退出但无任何已知形态命中，不确定（绝不默认判代码错），降级跳过",
        log_tail=log_text, details=details)


# ═══════════════════════════ 执行器（async） ═══════════════════════════

async def run_runtime_smoke(
    manager: Any,
    sandbox: Any,
    script: str,
    *,
    timeout_sec: int | None = None,
    language_key: str | None = None,
    prepare_timeout_sec: int | None = None,
    project_symbols: dict[str, Any] | None = None,
    probe_port: int | None = None,
    accept_budget_sec: int | None = None,
) -> RuntimeSmokeResult:
    """在沙箱内执行冒烟脚本并三分类（唯一通道 manager.run_command，禁 run_code）。

    infra 失败 ≠ 冒烟失败：run_command 异常 / __SMOKE_DONE__ 缺失 / envd 5xx →
    返 not_executed（skipped 语义，对齐 D31 ran/ok 区分）。
    prepare_timeout_sec（F1）：脚本含 prepare 阶段时的额外预算，计入 run_command timeout。
    accept_budget_sec（S2-5）：脚本含 assert 段时的断言执行预算（N 条 × 单条 max-time），
    计入 run_command timeout；缺省 None → 行为与现状一致。
    S2-5 透传契约：输出中含 `__ACCEPT_` 令牌的标记行【原样】收进 details.accept_output
    （本执行器不解析——解析/判定由 acceptance_spec 在 verify_runtime accept phase 侧做）。
    """
    window = timeout_sec if (isinstance(timeout_sec, int) and timeout_sec > 0) \
        else resolve_smoke_timeout_sec()
    prepare_budget = prepare_timeout_sec \
        if (isinstance(prepare_timeout_sec, int) and prepare_timeout_sec > 0) else 0
    accept_budget = accept_budget_sec \
        if (isinstance(accept_budget_sec, int) and accept_budget_sec > 0) else 0
    run_command = getattr(manager, "run_command", None)
    if run_command is None or sandbox is None:
        return RuntimeSmokeResult(
            "skipped", "not_executed", "沙箱执行通道不可用，冒烟未执行",
            details={"ran": False})
    try:
        # run_command 是同步阻塞调用，卸到线程池（与 verify.py 同款 to_thread，
        # contextvars 拷贝，沙箱上下文照常）。timeout = 探活窗口 + 收尾缓冲 + prepare 预算。
        result = await asyncio.to_thread(
            run_command, sandbox, script,
            timeout=window + RUN_TIMEOUT_BUFFER_SEC + prepare_budget + accept_budget,
            _skip_blacklist=True,
        )
    except Exception as exc:  # noqa: BLE001 — infra 异常一律未执行，不误判冒烟失败
        logger.warning("[RUNTIME_SMOKE] run_command 异常(infra)，冒烟未执行: %s",
                       str(exc)[:200])
        return RuntimeSmokeResult(
            "skipped", "not_executed",
            f"沙箱执行异常(infra)，冒烟未执行: {str(exc)[:200]}",
            details={"ran": False, "error": str(exc)[:500]})

    out = (getattr(result, "stdout", "") or "") + "\n" + (getattr(result, "stderr", "") or "")
    parsed = parse_smoke_markers(out)
    # S2-5：accept 标记行原样透传（只按令牌过滤行，不解析结构——phase 侧 parse_probe_output）
    accept_output = "\n".join(
        ln for ln in out.splitlines() if ACCEPT_MARK_TOKEN in ln)

    if parsed["probe_tool_missing"]:
        return RuntimeSmokeResult(
            "skipped", "probe_tool_missing",
            "沙箱内无可用探活工具(curl//dev/tcp/python3)，环境缺失不伪装代码失败",
            log_tail=parsed["log_tail"],
            details={"ran": True, "probe_tool": parsed["probe_tool"],
                     "timeout_sec": window})
    if not parsed["done"]:
        # 标记缺失 = 脚本没跑完（envd 5xx/连接断/超时截杀）→ 未执行，非冒烟失败
        return RuntimeSmokeResult(
            "skipped", "not_executed",
            "冒烟脚本结构化标记缺失(__SMOKE_DONE__)，判定为基础设施中断，未执行",
            details={"ran": False, "error": getattr(result, "error", None),
                     "raw_excerpt": out[-1000:], "timeout_sec": window})
    if parsed["port_busy"]:
        # F4：起应用【前】端口已有 listener → 环境问题（残留进程/同箱复用脏态），
        # 探活假绿风险由脚本侧提前退出根除，不起应用不判代码
        return RuntimeSmokeResult(
            "skipped", "port_busy",
            "起应用前推导端口已有 listener（环境残留），未起应用，跳过（环境问题非代码失败）",
            log_tail=parsed["log_tail"],
            details={"ran": True, "probe_tool": parsed["probe_tool"],
                     "probe_port": probe_port, "timeout_sec": window})
    prepare_rc = parsed.get("prepare_rc")
    if prepare_rc is not None and prepare_rc != 0:
        # F1：prepare（构建产物）失败——L2 已证编译通过，package 阶段失败大概率是
        # 插件/缓存/环境问题 → skipped 不冤枉代码；details 带 prepare 日志尾可观测
        logger.warning("[RUNTIME_SMOKE] prepare 命令失败(rc=%s)，冒烟未起应用 → skipped："
                       "L2 已证编译过，package 失败按环境处理", prepare_rc)
        return RuntimeSmokeResult(
            "skipped", "prepare_failed",
            f"构建产物 prepare 命令失败(rc={prepare_rc})，未起应用："
            "L2 已证编译通过，按环境问题跳过（不冤枉代码）",
            log_tail=parsed["log_tail"],
            details={"ran": True, "prepare_rc": prepare_rc,
                     "prepare_log_tail": parsed["log_tail"],
                     "timeout_sec": window})

    res = classify_smoke_outcome(
        parsed["app_rc"], parsed["log_tail"], parsed["probe_sequence"],
        language_key=language_key, project_symbols=project_symbols,
        probe_port=probe_port)
    res.details.update({
        "ran": True,
        "probe_tool": parsed["probe_tool"],
        "phases": parsed["phases"],
        "timeout_sec": window,
    })
    if prepare_rc is not None:
        res.details["prepare_rc"] = prepare_rc  # F1：prepare 成功也留痕（rc=0）
    if accept_output:
        # S2-5：断言证据原文透传（assert 段只在探活 ok 后执行，故只会出现在本路径）
        res.details["accept_output"] = accept_output
    return res
