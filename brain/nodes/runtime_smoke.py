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
# ★V-H2（B-4b）★ 端口反解窗口（秒）：应用 bind 端口远早于健康就绪，故独立且短于探活窗口。
# 反解不出即 fail-closed skipped，绝不占用整个冒烟预算去等一个可能永不出现的 listener。
# 端口反解窗口。★C-1 后语义变了★：不再是"最多等这么久"，而是**恒等满这么久**——
# 整窗取并集才能把错时 bind 的多监听判成 AMBIGUOUS（见 resolve_block）。故它同时是
# 反解路径的固定成本，做成 env 可调（登记 config/env_registry.py）。
# 够用性论证（reviewer 质疑 `cargo run`/`go run` 在窗口内还在编译，实测不成立）：
# 两条取箱臂都**先建过产物**——转交臂是 verify_l2 编译过的同一个箱；自建臂在
# `_acquire_smoke_sandbox` 里按 `detect_build_surface()` 跑一遍（实测 Cargo.toml →
# `cargo build -q`，go.mod → `go build ./...`，600s 预算）。`cargo build -q` 是 **debug**
# profile ＝ `cargo run` 复用的正是它 → 到冒烟时是增量重链，秒级 bind。
PORT_RESOLVE_WINDOW_SEC = 30
PORT_RESOLVE_WINDOW_ENV = "SWARM_SMOKE_PORT_RESOLVE_WINDOW_SEC"

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

# ★V-H2（B-4b）端口反解★ 端口推不出时不再直接 skip，改为【起进程后反解它实际监听的端口】。
# 值域：`<int>`（唯一端口→采用）| `AMBIGUOUS:<p1,p2,…>`（同进程树多监听，不猜）| `NONE`。
#
# 为什么这条一次救活一大片：`_FRAMEWORK_DEFAULT_PORTS` 里 **Rust 一个框架都没有**，Go 只有
# Gin —— echo/fiber/chi/net-http 全退化成裸 `"go"` → port None → 冒烟 100% skip。而
# `_derive_start_rust`/`_derive_start_go` **本来就推得出 start_cmd**，缺的只有端口。
# 与其逐个框架往表里塞默认端口（是猜，且永远追不完），不如问进程自己。
MARK_PORT_RESOLVED = "__SMOKE_PORT_RESOLVED__"
# 反解**用了哪一档工具**（复核 M-1）。值域 ss|netstat|lsof|proc|none_available。
# `none_available` ＝四档全废 → 那时"反解不出"必须报独立 reason，绝不与"应用没 bind"
# 共用一个名字（后者会让判读的人去查应用，而真相是我们没有探测工具）。
MARK_PORT_RESOLVE_TIER = "__SMOKE_PORT_RESOLVE_TIER__"
# 「哪一档**给出了答案**」（复核 M-1 第二半）——与上面的「哪一档**在场**」是两个事实：
# `ss` 在场却因 netns/权限返空时，答案可能来自 lsof//proc，也可能无人作答（none_answered）。
MARK_PORT_RESOLVE_TIER_ANSWERED = "__SMOKE_PORT_RESOLVE_TIER_ANSWERED__"

# ★M-2★ 反解未得端口时，哪些 classification 可以**直接采纳**：只收"纯由 log_tail 推出、
# 不预设探活发生过"的档。显式白名单＝枚举而非默认（fail-closed；新增档位必须在此显式登记，
# 否则落回 port_* 归因，绝不静默继承）。刻意排除：
#   `network_anomaly`——消息断言"探活不通"，而反解路径从未探活（会给出自信且错误的归因）；
#   `inconclusive`——语义正是"什么都没观测到"，该由 port_* 三档接手归因。
_LOG_DERIVED_CLASSIFICATIONS = frozenset({
    "code_error",                   # failed：确定性代码缺陷（H-1 的硬拦通道）
    "startup_crash_unattributed",   # skipped：崩了但归不到具体代码
    "env_missing",                  # skipped：含 PORT_BUSY 的 address already in use（O-3）
    "dependency_missing",           # skipped：项目外符号缺失（叠加 C-2 后才真正可辨）
})
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

# ★启动崩溃【已发生】但归因不明族（26 号文 C-6）★
# 病灶：本项目头号启动崩形态（Spring 的 BeanCreationException / APPLICATION FAILED TO
# START）被【刻意】排除在 _CODE_ERROR_PATTERNS 之外——那个决定是对的（它们常裹外部依赖
# 缺失，判 code_error 会把环境冤枉成代码）。但排除之后它们落进了 `inconclusive`，
# 而 inconclusive 的语义是"探活窗口耗尽/无任何已知形态命中"——于是
#   「应用明确崩了，我们看见了崩溃日志，只是不确定怪谁」
#   「什么都没发生，什么也没看到」
# 被写成了**同一个结果**，两者在交付面上都只是一行 skipped。
# 治法不是改判 failed（那会重新引入冤枉），是给它一个**自己的名字**：
# startup_crash_unattributed —— 仍 skipped（不阻断、不冤枉代码），但 degraded 可见、
# 消息里明说"启动确实崩了"，让交付面与复盘一眼可辨。
_STARTUP_CRASH_PATTERNS: dict[str, tuple[str, ...]] = {
    # ★必须有 generic 键（复核 HIGH，兄弟表 _ENV_MISSING/_BIND_SUCCESS 都有）★
    # `_match_family` 在 language_key 为空时【只回 generic】；而 language_key 来自
    # normalize_language_key(project_stack.backend)，detect_stack 失手/前端型/多栈项目即 None
    # → 整族静默失效（实测 lang=None + "APPLICATION FAILED TO START" 仍落 inconclusive）。
    # 跨栈字面（不含语言假设）放这里。
    "generic": (
        r"APPLICATION FAILED TO START",
    ),
    "java": (
        r"APPLICATION FAILED TO START",
        r"BeanCreationException",
        r"BeanInstantiationException",
        r"UnsatisfiedDependencyException",
        r"BeanDefinitionStoreException",
        r"ApplicationContextException",
    ),
    "node": (
        r"\bUnhandledPromiseRejection\b",
        r"code:\s*'ERR_",
    ),
    "python": (
        r"Traceback \(most recent call last\)",
    ),
    "go": (
        # ★不用 `^` 锚（复核 HIGH）★：log_text 是 `tail -n 200` 的日志尾，几乎不可能以
        # panic 起始；而 _match_family 此处 flags=0，`^` 只匹配字符串开头 → 对 go 完全没生效。
        # 两个透镜都实测：真实多行 panic 落 inconclusive，而测试喂的单行形态恰好在 index 0。
        r"\bpanic: ",
    ),
    "rust": (
        r"thread '.*' panicked at",
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


# ★V-H2 端口反解 shell 段★ 只在 `port is None` 时注入。四条设计要点都有实测依据：
# ① **按进程树限定**：沙箱里可能有别的 listener（前轮残留/sidecar）。全局扫端口会把无关
#    listener 当成"应用起来了"＝假绿。只认 `$SMOKE_PID` 及其后代持有的 socket。
#    （实测：父不监听、子监听的 wrapper→server 形态必须靠树遍历才找得到。）
# ② **工具四级降级**：ss → netstat → lsof → `/proc/net/tcp`+`/proc/<pid>/fd`。末条零外部
#    依赖，是最小容器里唯一可用的路径（与探活工具自适应同款口径）。
# ③ **多监听 fail-closed**：同树监听多端口（app + metrics）→ 报 AMBIGUOUS，不猜。
# ④ **每个 helper 自吞 stderr**：日志尾要喂崩溃分类器，探测噪声混进去＝给分类器喂假证据
#    （实测 macOS netstat 无 -p 时 awk 会吐 "newline in string"）。
_PORT_RESOLVE_FUNCS = """smoke_pid_tree() {
  local root="$1" queue="$1" out="" cur kids
  # ★procps 缺席兜底（复核 C-2）★ 沙箱镜像不装 procps；而 `bash -c '<单条简单命令>'`
  # 会 exec 优化 → SMOKE_PID 就是 cargo/go 本体，而 `cargo run`/`go run` 的真监听者
  # **恒是它们 fork 的子进程** → 树遍历是 V-H2 目标人群的**必经步骤**，不是可选优化。
  # `pkill -P` 早就软依赖 procps，但清理失败无害、从不承重；V-H2 让同一缺失工具**换了
  # 后果档**（"复用单一事实源 ≠ 复用其消费契约"的又一形态）。
  #
  while [ -n "$queue" ]; do
    cur="${queue%% *}"
    if [ "$cur" = "$queue" ]; then queue=""; else queue="${queue#* }"; fi
    [ -z "$cur" ] && continue
    out="$out $cur"
    kids=$(pgrep -P "$cur" 2>/dev/null | tr '\\n' ' ')
    # PPid 取法：comm 可含空格/括号 → 从**最后一个** `)` 之后再切字段（$1=state $2=ppid）。
    # ★M-3①★ `read` 内建替代 `$(cat …)`：命令替换 ＝ 每文件一次 fork，而本循环是
    # 「树内 pid 数 × 全系统进程数 × 每 2s 一轮」→ 窗口可能被扫描自身吃光（误杀方向）。
    # `/proc/*/stat` 恒单行，行为等价、零 fork。
    #
    # ★为什么**不**改成"pgrep 在场就不进这个分支"（试过并回退，别再重做）★
    # 那样确实省掉"叶子节点也扫一遍全 /proc"的开销，但把 `pgrep` 从"软依赖"升成"权威"：
    # busybox/裁剪版 procps 的 `pgrep -P` 不支持或行为异常时，空输出会被当成"确实没有子进程"
    # → 树退化成根 pid → C-2 原病复发（`cargo run` 的真监听者是子进程）。
    # 「空输出」与「工具不可用」在这里不可分，而**误判方向是致命的那一侧**，故保留
    # "空即兜底"。开销问题由上面的 `read` 内建解掉（纯 builtin，无 fork）。
    if [ -z "$kids" ]; then
      for _st in /proc/[0-9]*/stat; do
        [ -r "$_st" ] || continue
        _line=""
        read -r _line < "$_st" 2>/dev/null
        [ -n "$_line" ] || continue
        _cpid=${_line%% *}                 # 字段1＝pid（取自行内，不从路径反推）
        _after=${_line##*\\) }              # comm 可含空格/括号 → 从最后一个 `) ` 之后切
        set -- $_after
        [ "${2:-}" = "$cur" ] || continue   # $1=state $2=PPid
        kids="$kids $_cpid"
      done
    fi
    [ -n "$kids" ] && queue="$queue $kids"
  done
  echo "$out" | tr ' ' '\\n' | grep -E '^[0-9]+$' | sort -u
}
smoke_ports_ss() {
  command -v ss >/dev/null 2>&1 || return 1
  local p out=""
  for p in $1; do
    out="$out $(ss -ltnpH 2>/dev/null | awk -v pid="$p" '$0 ~ ("pid="pid",") {print $4}' 2>/dev/null)"
  done
  echo "$out" | tr ' ' '\\n' | sed 's/.*://' | grep -E '^[0-9]+$' | sort -u
}
smoke_ports_netstat() {
  command -v netstat >/dev/null 2>&1 || return 1
  local p out=""
  for p in $1; do
    # 扫**全行**而非 $NF（复核 M-3）：真实输出 `1234/nginx: master` 的 $NF 是 `master` → 失配。
    out="$out $(netstat -ltnp 2>/dev/null | awk -v pid="$p" '$0 ~ ("(^| )"pid"/") {print $4}' 2>/dev/null)"
  done
  echo "$out" | tr ' ' '\\n' | sed 's/.*://' | grep -E '^[0-9]+$' | sort -u
}
smoke_ports_lsof() {
  command -v lsof >/dev/null 2>&1 || return 1
  local csv
  csv=$(echo "$1" | tr '\\n' ',' | sed 's/,$//')
  [ -z "$csv" ] && return 1
  lsof -nP -iTCP -sTCP:LISTEN -a -p "$csv" 2>/dev/null \
    | awk 'NR>1 {print $9}' 2>/dev/null | sed 's/.*://' | grep -E '^[0-9]+$' | sort -u
}
smoke_ports_proc() {
  [ -r /proc/net/tcp ] || return 1
  local p fd ln inodes="" f
  for p in $1; do
    for fd in /proc/"$p"/fd/*; do
      ln=$(readlink "$fd" 2>/dev/null) || continue
      case "$ln" in socket:\\[*\\]) inodes="$inodes ${ln#socket:[}";; esac
    done
  done
  inodes=$(echo "$inodes" | tr -d ']' | tr ' ' '\\n' | grep -E '^[0-9]+$' | sort -u)
  [ -z "$inodes" ] && return 1
  for f in /proc/net/tcp /proc/net/tcp6; do
    [ -r "$f" ] || continue
    tail -n +2 "$f" 2>/dev/null | while read -r _sl laddr _rest_line; do
      set -- $_rest_line
      [ "$2" = "0A" ] || continue
      # ★字段序（复核 C-1：原取 $10 是 sk 指针，该档在任何真 Linux 上恒返空）★
      # `read -r _sl laddr _rest_line` 已吃掉前 2 列（sl、local_address）→ `_rest_line` 的
      # $N ＝绝对第 N+2 列。内核 get_tcp4_sock 绝对列序：sl(1) local(2) rem(3) st(4)
      # tx:rx(5) tr:when(6) retrnsmt(7) uid(8) timeout(9) inode(10)
      # → 换算进 `_rest_line`：rem=$1 st=$2 … uid=$6 timeout=$7 **inode=$8**；
      # $10 ＝绝对第 12 列 ＝ `%pK` sk 指针
      # （kptr_restrict=1 恒 16 个 0；放开也是十六进制指针，永不等于十进制 inode——两种
      # 配置都确定失败，不是概率性）。
      _inode=$(echo "$_rest_line" | awk '{print $8}')
      echo "$inodes" | grep -qx "$_inode" || continue
      printf '%d\\n' "0x${laddr##*:}" 2>/dev/null
    done
  done | grep -E '^[0-9]+$' | sort -u
}
smoke_resolve_tier() {
  # ★哪一档工具**在场**（复核 M-1 第一半）★ 四档全无时，"反解不出"的真相是"探测工具全废"，
  # 而不是"应用没 bind"——两者共用一个 reason 会给出**自信且错误**的结论（B-4a
  # CRITICAL-3 同族）。探活侧有 MARK_PROBE_TOOL 输出用了哪个工具，反解侧也得有。
  # ★注意语义边界（reviewer 复核 M-1 第二半）★ 本函数只答"在场"，**不答"谁给出了答案"**：
  # `ss` 在场但因 netns/权限/容器视图返空时，实际答案来自 lsof 或 /proc，甚至无人作答。
  # 「谁作答」由 smoke_resolve_port 落 $SMOKE_TIER_FILE，经 MARK_PORT_RESOLVE_TIER_ANSWERED
  # 单独携出——**两个键分开，别拿在场档冒充作答档**（那正是"自信且错误"的另一形态）。
  if command -v ss >/dev/null 2>&1; then echo ss
  elif command -v netstat >/dev/null 2>&1; then echo netstat
  elif command -v lsof >/dev/null 2>&1; then echo lsof
  elif [ -r /proc/net/tcp ]; then echo proc
  else echo none_available; fi
}
smoke_resolve_port() {
  local pids ports n _t=""
  pids=$(smoke_pid_tree "$1")
  [ -z "$pids" ] && { echo "NONE"; return 1; }
  # 四级降级：每档【自己是否作答】就地记档（复核 M-1）——本函数在 $(…) 子壳里跑，
  # 变量回不到父壳，故落文件（$SMOKE_TIER_FILE 由 resolve_block 先建）。
  ports=$(smoke_ports_ss "$pids"); [ -n "$ports" ] && _t=ss
  [ -z "$ports" ] && { ports=$(smoke_ports_netstat "$pids"); [ -n "$ports" ] && _t=netstat; }
  [ -z "$ports" ] && { ports=$(smoke_ports_lsof "$pids"); [ -n "$ports" ] && _t=lsof; }
  [ -z "$ports" ] && { ports=$(smoke_ports_proc "$pids"); [ -n "$ports" ] && _t=proc; }
  ports=$(echo "$ports" | grep -E '^[0-9]+$' | sort -u)
  [ -z "$ports" ] && { echo "NONE"; return 1; }
  [ -n "$_t" ] && [ -n "${SMOKE_TIER_FILE:-}" ] && printf '%s' "$_t" > "$SMOKE_TIER_FILE" 2>/dev/null
  n=$(echo "$ports" | wc -l | tr -d ' ')
  if [ "$n" != "1" ]; then
    echo "AMBIGUOUS:$(echo "$ports" | tr '\\n' ',' | sed 's/,$//')"; return 1
  fi
  echo "$ports"
}
"""


def build_smoke_script(
    start_cmd: str,
    port: int | str | None,
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
    # ★V-H2★ port=None ＝"推不出，起进程后反解"。已知端口路径**逐字节不变**
    # （resolve_funcs/resolve_block 为空串、F4 预检照旧、SMOKE_PORT 直接赋值）。
    resolve_port = port is None
    port_num = 0 if resolve_port else int(port)
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
    resolve_funcs = _PORT_RESOLVE_FUNCS if resolve_port else ""
    # F4 端口预检**只在端口已知时**可做（未知端口无从预检）。已知路径逐字节不变。
    # 诚实边界：反解路径因此没有 PORT_BUSY 保护——但按进程树限定（①）保证不会误采残留
    # listener；代价是"应用因端口被占起不来"退化成 resolve=NONE → skipped，而非明确 PORT_BUSY。
    port_busy_block = "" if resolve_port else f"""if [ -n "$PROBE_TOOL" ] && smoke_probe_once; then
  echo "{MARK_PORT_BUSY}"
  echo "{MARK_PHASE}collect"
  echo "{MARK_LOG_BEGIN}"
  echo "{MARK_LOG_END}"
  echo "{MARK_DONE}"
  exit 0
fi
"""
    # ★反解段必须在 `trap smoke_cleanup` **之后**★ 它会 `exit 0`；放在 trap 之前，
    # 反解失败退出时应用进程会泄漏（自己写的时候就踩了一次）。
    resolve_block = ""
    if resolve_port:
        resolve_block = f"""echo "{MARK_PHASE}resolve_port"
SMOKE_TIER_FILE="${{TMPDIR:-/tmp}}/.swarm_smoke_tier.$$"
: > "$SMOKE_TIER_FILE" 2>/dev/null || SMOKE_TIER_FILE=""
SMOKE_RESOLVE_DEADLINE=$(( $(date +%s) + {_resolve_positive_int_env(
    PORT_RESOLVE_WINDOW_ENV, PORT_RESOLVE_WINDOW_SEC)} ))
# ★C-1（reviewer 复核，CRITICAL 假过）★ 绝不"首轮看到一个端口就采信"。
# 旧写法 `case … [0-9]*) break` 让"多监听 fail-closed"退化成**竞态**：只有当两个 listener
# 在**同一次 2s 轮询的瞬间**都已 bind 时才判 AMBIGUOUS。而真实形态常是**错时**的——
# metrics/admin 端口在进程起来就 bind，业务 server 要等 async runtime 就绪/DB 连上才 bind。
# 于是首轮只看见 metrics → 采纳它 → 探 `/` 得 404 → classify 判 `passed:started`，
# **业务 server 一次都没被探过**，第三道确定性闸报通过（新引入的假过通道，非旧病）。
# 治法：**整窗取并集**，窗口结束才裁决（0→NONE / 1→采纳 / ≥2→AMBIGUOUS）。
# 代价＝反解路径恒付满窗口（已在预算内：窗口+探活+收尾 < run_command timeout）；
# 收益＝§7.10 声称的"多监听不猜"从竞态承诺变成**真承诺**。
# 刻意**不**改成"反解路径只认 2xx/3xx"（考虑过并否决）：裸 API 对 `/` 返 404 合法且常见，
# 已知端口路径也接受它——只收窄反解一侧＝对同一应用形态双标，误杀方向。
SMOKE_PORT_SEEN=""
while [ "$(date +%s)" -lt "$SMOKE_RESOLVE_DEADLINE" ]; do
  if ! kill -0 "$SMOKE_PID" 2>/dev/null; then break; fi
  SMOKE_PORT_CUR=$(smoke_resolve_port "$SMOKE_PID")
  case "$SMOKE_PORT_CUR" in
    AMBIGUOUS:*)
      # ≥2 已成立，再等只会更多 → 提前收（把已见集合并入，保留全部证据）
      SMOKE_PORT_SEEN="$SMOKE_PORT_SEEN $(echo "${{SMOKE_PORT_CUR#AMBIGUOUS:}}" | tr ',' ' ')"
      break ;;
    [0-9]*) SMOKE_PORT_SEEN="$SMOKE_PORT_SEEN $SMOKE_PORT_CUR" ;;
  esac
  sleep "$SMOKE_INTERVAL"
done
SMOKE_PORT_SEEN=$(echo "$SMOKE_PORT_SEEN" | tr ' ' '\\n' | grep -E '^[0-9]+$' | sort -u)
if [ -z "$SMOKE_PORT_SEEN" ]; then
  SMOKE_PORT_RAW="NONE"
elif [ "$(echo "$SMOKE_PORT_SEEN" | wc -l | tr -d ' ')" = "1" ]; then
  SMOKE_PORT_RAW="$SMOKE_PORT_SEEN"
else
  SMOKE_PORT_RAW="AMBIGUOUS:$(echo "$SMOKE_PORT_SEEN" | tr '\\n' ',' | sed 's/,$//')"
fi
echo "{MARK_PORT_RESOLVE_TIER}$(smoke_resolve_tier)"
SMOKE_TIER_ANSWERED=$(cat "$SMOKE_TIER_FILE" 2>/dev/null)
[ -n "$SMOKE_TIER_ANSWERED" ] || SMOKE_TIER_ANSWERED=none_answered
[ -n "$SMOKE_TIER_FILE" ] && rm -f "$SMOKE_TIER_FILE" 2>/dev/null
echo "{MARK_PORT_RESOLVE_TIER_ANSWERED}$SMOKE_TIER_ANSWERED"
echo "{MARK_PORT_RESOLVED}$SMOKE_PORT_RAW"
case "$SMOKE_PORT_RAW" in
  [0-9]*) SMOKE_PORT="$SMOKE_PORT_RAW" ;;
  *)
    # 反解不出/歧义 → 不猜端口、不探活（探未知端口必 refused=假红）。收尾标记照常输出，
    # 上层按 skipped 处理（fail-closed，绝不把"我们没探"伪装成"启动失败"）。
    echo "{MARK_PHASE}collect"
    echo "{MARK_LOG_BEGIN}"
    tail -n {tail_n} "$SMOKE_LOG" 2>/dev/null
    echo "{MARK_LOG_END}"
    # ★M-4（reviewer 复核）★ 与收尾块同构：应用**已退出**时也要回显退出码。
    # 原写法只在 alive 时输出 → 已死时 app_rc=None，退出码这个归因信号整条丢失
    # （alive/dead 仍可辨，但"崩在第几步"的证据没了；已知端口路径一直有）。
    if kill -0 "$SMOKE_PID" 2>/dev/null; then
      echo "{MARK_APP_RC}alive"
    else
      wait "$SMOKE_PID"
      echo "{MARK_APP_RC}$?"
    fi
    echo "{MARK_DONE}"
    exit 0 ;;
esac
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
{resolve_funcs}cd {q_workdir} || {{ echo "{MARK_PHASE}workdir_unavailable"; exit 96; }}
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
# ★探活必须取回 HTTP 状态码（26 号文 C-5）★
# 原判据是 `curl -s -o /dev/null`——**没有 -f**，curl 对 500 一样退 0。于是"应用起来了但
# 每个接口都 500"与"应用健康"在闸门眼里完全相同，"真启动"实际只证明了"端口通"。
# 这里把状态码写进 SMOKE_HTTP_CODE，由上层分类器裁决；TCP 兜底路径（无 curl）如实
# 置 000 并在结果里标注"本轮只做了端口探测"，绝不让降级探测冒充 HTTP 校验。
SMOKE_HTTP_CODE=""
smoke_probe_once() {{
  SMOKE_HTTP_CODE=""
  case "$PROBE_TOOL" in
    curl)
      SMOKE_HTTP_CODE=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time 2 \
        "http://127.0.0.1:${{SMOKE_PORT}}${{SMOKE_HEALTH}}" 2>/dev/null) || return 1
      [ -n "$SMOKE_HTTP_CODE" ] && [ "$SMOKE_HTTP_CODE" != "000" ] ;;
    devtcp) (exec 3<>"/dev/tcp/127.0.0.1/${{SMOKE_PORT}}") 2>/dev/null ;;
    python3) python3 -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',${{SMOKE_PORT}}))==0 else 1)" ;;
    *) return 1 ;;
  esac
}}
{port_busy_block}{prepare_block}echo "{MARK_PHASE}start"
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
{resolve_block}echo "{MARK_PHASE}probe"
SMOKE_OK=0
if [ -n "$PROBE_TOOL" ]; then
  SMOKE_DEADLINE=$(( $(date +%s) + SMOKE_TIMEOUT ))
  while [ "$(date +%s)" -lt "$SMOKE_DEADLINE" ]; do
    if ! kill -0 "$SMOKE_PID" 2>/dev/null; then
      echo "{MARK_PROBE}exited"
      break
    fi
    if smoke_probe_once; then
      echo "{MARK_PROBE}ok:${{SMOKE_HTTP_CODE:-none}}"
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


def _strip_app_log_region(out: str) -> str:
    """剔掉 stdout 里的【被测应用日志】区段（MARK_LOG_BEGIN…MARK_LOG_END 之间）。

    ★回显注入面（26 号文 I-M1）★
    探活序列原先在【全量 stdout】上 findall——而脚本尾部会把应用自己的日志 tail 进来。
    应用只要打印一行字面 `__SMOKE_PROBE__ok`（日志里回显请求 URL、框架 banner、
    甚至恶意注入），就被计入探活序列 → 直接判 passed。
    兄弟机制 `acceptance_spec.parse_probe_output` 早已为同一注入面做了 F9 加固
    （首标记占位、后到伪造行不覆盖），smoke 侧一直没做——**家族不对称**正是本仓
    "修一类先全仓捞 sibling"纪律要防的形态。
    这里用脚本自己的分隔标记做确定性切除：控制面标记只认日志区【之外】的。
    ★收尾必须取【最后一个】END（对抗双复核独立实证的 CRITICAL）★
    初版用 `partition` 取第一个 END——而日志区的内容**完全由被测应用控制**：应用打一行
    含 `__SMOKE_LOG_TAIL_END__` 的字样（回显请求 URL/query、框架 banner、恶意注入），
    控制面就被重新打开，其后的 `__SMOKE_PROBE__ok:200` 照样生效。两个复核透镜各自实测
    伪造出 `passed/started`——**本修复对它自称的威胁模型不成立**，与本轮"治本自己也会
    静默失效"的元教训同型。
    真 END 恒是全文最后一个：应用只能写进被 `tail -n` 收割的日志文件，物理上无法在真 END
    之后输出（脚本里 END 之后只剩 `echo MARK_DONE`）。故 `rpartition`。
    未闭合（脚本被掐断）→ 从 BEGIN 起全部视作日志区（fail-closed：宁可少认几个探活
    标记判 inconclusive，也绝不把应用回显当探活成功）。
    """
    if MARK_LOG_BEGIN not in out:
        return out
    head, _, rest = out.partition(MARK_LOG_BEGIN)
    _, sep, tail = rest.rpartition(MARK_LOG_END)
    return head + (tail if sep else "")


def parse_smoke_markers(output: str) -> dict[str, Any]:
    """从沙箱 stdout+stderr 解析结构化标记（纯函数）。"""
    out = output or ""
    # I-M1：控制面标记只在【应用日志区之外】解析，防被测应用回显伪造探活成功
    ctrl = _strip_app_log_region(out)
    # `ok:200` 形态需带冒号——`\w+` 会把状态码切掉，令 C-5 的 HTTP 校验静默退化回端口闸
    probe_sequence = re.findall(rf"{MARK_PROBE}([\w:]+)", ctrl)
    tool_m = re.search(rf"{MARK_PROBE_TOOL}(\w+)", ctrl)
    probe_tool = tool_m.group(1) if tool_m else None
    rc_m = re.search(rf"{MARK_APP_RC}(alive|-?\d+)", ctrl)
    app_rc: str | int | None
    if rc_m is None:
        app_rc = None
    elif rc_m.group(1) == "alive":
        app_rc = "alive"
    else:
        app_rc = int(rc_m.group(1))
    b = out.find(MARK_LOG_BEGIN)
    e = out.rfind(MARK_LOG_END)      # 同上：取最后一个，否则应用回显一行 END 就截断崩溃证据
    log_tail = out[b + len(MARK_LOG_BEGIN):e].strip("\n") if (b != -1 and e > b) else ""
    # I-M1：其余控制面标记同源——都只认日志区之外（port_busy/done 是判据，phases 是相位机
    # 状态，被应用回显伪造同样有害；log_tail 本身当然仍从原文取）。
    prep_m = re.search(rf"{MARK_PREPARE_RC}(-?\d+)", ctrl)
    return {
        "probe_sequence": probe_sequence,
        "probe_tool": probe_tool,
        "probe_tool_missing": (MARK_PROBE_TOOL_MISSING in ctrl) or (probe_tool == "MISSING"),
        "app_rc": app_rc,
        "prepare_rc": int(prep_m.group(1)) if prep_m else None,   # F1
        "port_busy": MARK_PORT_BUSY in ctrl,                       # F4
        # ★V-H2★ 反解结果。**从 ctrl（剥掉应用日志区）取**——与其它控制面标记同源：
        # 被测应用回显一行伪造的 `__SMOKE_PORT_RESOLVED__80` 就能把探活指向别的端口
        # （I-M1 那族）。`None`=本轮未走反解（端口已知）。
        # 反解用了哪一档工具（复核 M-1）。`none_available` ＝四档全废，那时"反解不出"
        # 必须报独立 reason，绝不与"应用没 bind"共用一个名字。
        "port_resolve_tier": (t.group(1)
                              if (t := re.search(rf"{MARK_PORT_RESOLVE_TIER}(\w+)", ctrl))
                              else None),
        # 「谁**作答**」独立键（复核 M-1）：在场≠作答。两个键的正则互不误配——
        # 在场档要求字面 `TIER__`，作答档是 `TIER_ANSWERED__`（`TIER_` 后接 `A` 非 `_`）。
        "port_resolve_tier_answered": (
            a.group(1)
            if (a := re.search(rf"{MARK_PORT_RESOLVE_TIER_ANSWERED}(\w+)", ctrl))
            else None),
        "port_resolved": (m.group(1)
                          if (m := re.search(rf"{MARK_PORT_RESOLVED}([\w:,]+)", ctrl))
                          else None),
        "log_tail": log_tail,
        "phases": re.findall(rf"{MARK_PHASE}(\w+)", ctrl),
        "done": MARK_DONE in ctrl,
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
    # ★探活 ok 只是"有人应答"，还要看【应答了什么】（26 号文 C-5）★
    # 标记形态：`ok`（TCP 兜底，无状态码）| `ok:<http_code>`（curl 路径）。
    _ok_tokens = [t for t in probe_sequence if t == "ok" or t.startswith("ok:")]
    _http_code: str | None = None
    for _t in _ok_tokens:
        if ":" in _t:
            _http_code = _t.split(":", 1)[1]
    if _http_code and _http_code not in ("none", "000"):
        details["probe_http_code"] = _http_code
    if _ok_tokens:
        if app_rc == "alive":
            # 5xx = 服务起来了但在报错。**刻意不判 failed**：沙箱内无 DB/无外部服务，
            # 500 极可能是环境缺失（fail-honest 铁律：环境绝不伪装代码失败）。但也**绝不
            # 判 passed**——原判据 `curl -s -o /dev/null` 没有 -f，对 500 一样退 0，于是
            # "每个接口都 500"与"应用健康"在闸门眼里完全相同，"真启动"实际只证明了端口通。
            # 独立 outcome + degraded，让交付面看得见"这次没验到应用真的能服务"。
            if _http_code and _http_code[:1] == "5":
                details["degraded"] = True
                logger.warning(
                    "[RUNTIME_SMOKE] 探活应答 HTTP %s（5xx）——服务在跑但健康端点报错。"
                    "沙箱无外部依赖时 5xx 常为环境所致，故不判代码失败；但也绝不判通过",
                    _http_code)
                return RuntimeSmokeResult(
                    "skipped", "http_server_error",
                    f"应用已监听且应答，但健康端点返回 HTTP {_http_code}（5xx）——"
                    "服务未真正可用；沙箱缺外部依赖时 5xx 常为环境所致，不判代码失败也不判通过",
                    log_tail=log_text, details=details)
            if not _http_code or _http_code in ("none", "000"):
                # curl 缺席 → 只做了 TCP 连通性探测。这不是 HTTP 校验，如实标注 degraded，
                # 别让降级探测冒充"真启动"（北极星的第三道确定性闸就是靠它立信）。
                details["degraded"] = True
                details["probe_depth"] = "tcp_only"
                logger.warning(
                    "[RUNTIME_SMOKE] 环境无 curl → 本轮只做了 TCP 端口探测（未校验 HTTP "
                    "状态码）：通过结论的强度低于 HTTP 校验，已标 degraded")
                return RuntimeSmokeResult(
                    "passed", "started_tcp_only",
                    "运行时冒烟通过（**仅端口探测**：环境无 curl，未校验 HTTP 状态码）",
                    log_tail=log_text, details=details)
            return RuntimeSmokeResult(
                "passed", "started",
                f"运行时冒烟通过：应用启动且健康端点应答 HTTP {_http_code}",
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
    # C-6：崩溃【已发生】≠ 什么都没观测到。给它自己的名字，别与"窗口耗尽"混为一谈。
    crash_hits = _match_family(_STARTUP_CRASH_PATTERNS, language_key, log_text)
    if crash_hits:
        details["startup_crash_hits"] = crash_hits
        logger.warning(
            "[RUNTIME_SMOKE] 启动确实崩溃（命中 %s）但归因不明——该族常裹外部依赖缺失，"
            "判 code_error 会把环境冤枉成代码，故仍 skipped；但绝不与"
            "'什么都没观测到'共用 inconclusive", crash_hits[:3])
        return RuntimeSmokeResult(
            "skipped", "startup_crash_unattributed",
            f"应用启动过程中确实崩溃（命中 {crash_hits[:3]}），但该形态常裹外部依赖缺失，"
            "无法确定性归因到代码——不判失败（绝不冤枉代码），也绝不当作'未观测到问题'",
            log_tail=log_text, details=details)
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
    # ★V-H2★ 端口反解结果裁决。判序：在 prepare/probe 之前——反解不出时脚本根本没探活，
    # 拿 probe_sequence 去分类会得出"timeout"这种**错误且自信**的结论（实际是我们没探）。
    _pr = parsed.get("port_resolved")
    _resolved_port: int | None = None
    if _pr is not None:
        if _pr.isdigit():
            # 反解成功 → 用真实端口覆盖 probe_port（None），让下游"端口推导错配"提示与
            # 断言 evidence 指向真端口，而不是继续显示 None。
            probe_port = int(_pr)
            # ★H-2（reviewer 复核）★ 只喂分类器不落账＝checkpoint 上没有"这次在哪个端口
            # 验过"的机读证据（L-1 本该治的，但那条测试写成 `== 54321 or status=="passed"`
            # ——`or` 恒真，把机制删掉它照旧绿）。见下方 res.details 无条件回填。
            _resolved_port = probe_port
        else:
            # ★H-1（复核）：先过崩溃分类器，再落 skipped★
            # 我原来的"判序在 probe 分类之前"论证只对 `probe_sequence` 成立（确实没探活），
            # 但**过度纠正**了：`_CODE_ERROR_PATTERNS`/`_STARTUP_CRASH_PATTERNS` 只吃
            # `log_tail`，与 probe_sequence 无关，而 log_tail 在本分支里**已经收割在手**。
            # 不过一遍分类器 → V-H2 对目标栈**结构上产不出 `failed`**，而"启动就崩"恰是
            # 这道闸存在的头号理由（与 C-6 原话"崩溃已发生 ≠ 什么都没观测到"直接冲突）。
            # ★C-2（reviewer 复核，CRITICAL）★ `project_symbols` 必须传——漏了它，
            # `_symbol_is_project_internal` 恒返 None → 项目内相对 import 缺失（worker 漏建
            # 本地文件的常见形态）落 `dependency_missing`(skipped) 而非 `code_error`(failed)，
            # 于是 H-1 承诺的"硬拦通道"对整个 import 族**结构上不可达**（差一个 kwarg）。
            # 已知端口路径一直传（见下方同名调用）——**不对称就是漏接线**。
            _tier = parsed.get("port_resolve_tier")
            _tier_answered = parsed.get("port_resolve_tier_answered")
            _cls = classify_smoke_outcome(
                parsed["app_rc"], parsed["log_tail"], [], language_key=language_key,
                project_symbols=project_symbols)
            # ★M-2（reviewer 复核；比原登记 O-3 宽两格）★ 原来只采纳 `status=="failed"`，
            # 于是 `env_missing`（含 PORT_BUSY 的 `address already in use`）、
            # `startup_crash_unattributed`、`dependency_missing` 三类**已观测形态**全被洗成
            # `port_unresolved`——status 同为 skipped 故 auto_accept 无差，但归因指向"应用没
            # bind"而日志明写别的，与 C-6 原话"崩溃已发生 ≠ 什么都没观测到"冲突。
            # 采纳集是**显式白名单**（枚举而非默认，fail-closed）：只收"纯由日志尾推出"的档。
            # `network_anomaly` 刻意**不**收——它的消息断言"探活不通"，而反解路径从未探活；
            # `inconclusive` 不收——那正是"什么都没观测到"，该由下面的 port_* 归因接手。
            if _cls.classification in _LOG_DERIVED_CLASSIFICATIONS:
                _cls.details.update({
                    "ran": True,
                    "port_resolved_raw": _pr,
                    "port_resolve_tier": _tier,
                    "port_resolve_tier_answered": _tier_answered,
                    "timeout_sec": window,
                })
                logger.warning(
                    "[RUNTIME_SMOKE] V-H2 端口反解未得端口(%s)，但日志尾命中确定性形态(%s/%s)"
                    " → 如实按该形态裁决（绝不因'没探活'把已观测到的事实降级成"
                    "'应用没 bind'）", _pr, _cls.status, _cls.classification)
                return _cls
            if _pr.startswith("AMBIGUOUS"):
                _reason = "port_ambiguous"
            elif _tier == "none_available":
                # ★M-1★ 四档探测工具全废 ≠ 应用没 bind。共用一个 reason 会让判读的人去查
                # 应用，而真相是我们没有探测手段（沙箱镜像不装 iproute2/net-tools/lsof，
                # 且 /proc 不可读时连保底档也没有）。
                _reason = "port_resolve_tooling_missing"
            else:
                _reason = "port_unresolved"
            if _pr.startswith("AMBIGUOUS"):
                _detail = f"同一进程树监听多个端口（{_pr.split(':', 1)[1]}），不猜"
            elif _reason == "port_resolve_tooling_missing":
                _detail = ("沙箱内四档端口探测工具全不可用（ss/netstat/lsof 未装且 "
                           "/proc/net/tcp 不可读）——这是**环境缺探测手段**，不是应用没监听")
            else:
                _detail = "进程树内无任何 TCP listener（可能启动即退/未 bind/仅 unix socket）"
            logger.warning("[RUNTIME_SMOKE] V-H2 端口反解未得唯一端口(%s) → skipped：%s",
                           _pr, _detail)
            # 未采纳分类器结论时，它已收割的**证据键**照样并入（不覆盖本层同名键）——
            # 典型是 `network_anomaly` 的 `bind_success_hits`："日志自报 bind 成功却反解不到
            # 端口"这条矛盾是判读的关键线索，丢了它就只剩"应用没 bind"一句错话。
            _details = {"ran": True, "probe_tool": parsed["probe_tool"],
                        "port_resolved_raw": _pr, "port_resolve_tier": _tier,
                        "port_resolve_tier_answered": _tier_answered,
                        "app_rc": parsed["app_rc"], "timeout_sec": window}
            for _k, _v in (_cls.details or {}).items():
                _details.setdefault(_k, _v)
            return RuntimeSmokeResult(
                "skipped", _reason,
                f"端口推导不出且反解未得唯一端口（{_pr}）：{_detail}——未探活，"
                "按环境/推导缺口跳过（fail-closed，绝不把'我们没探'判成启动失败）",
                log_tail=parsed["log_tail"], details=_details)
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
    if _resolved_port is not None:
        # ★H-2★ 反解路径无条件落账：这是"本次在哪个端口验过"的唯一机读证据。
        # 只在反解路径写（已知端口路径的 details 形状不在本批范围内，别顺手改）。
        res.details["probe_port"] = _resolved_port
        res.details["port_resolve_tier_answered"] = parsed.get("port_resolve_tier_answered")
    if prepare_rc is not None:
        res.details["prepare_rc"] = prepare_rc  # F1：prepare 成功也留痕（rc=0）
    if accept_output:
        # S2-5：断言证据原文透传（assert 段只在探活 ok 后执行，故只会出现在本路径）
        res.details["accept_output"] = accept_output
    return res
