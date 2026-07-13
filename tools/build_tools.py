"""构建与测试 Tool 集 — run_command / run_compile / run_tests

支持两种执行模式：
  1. 本地模式（默认）— subprocess 执行
  2. 远程沙箱模式 — 在 CubeSandbox (E2B) 中执行

run_command 使用白名单制，不在白名单中的命令会被拒绝。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from swarm.config.settings import WorkerConfig, get_config


def _workspace_root() -> Path:
    from swarm.tools.paths import workspace_root
    return workspace_root()


def _worker_config() -> WorkerConfig:
    return get_config().worker


# ── 远程沙箱上下文（ContextVar：并发 worker 间隔离，杜绝跨 worker 串味，S1 修复）──
# 原为模块级全局变量，asyncio.gather 真并发时 Worker B 的 set 会盖掉 Worker A，
# 导致 A 的 build/test/L1/文件写入打到 B 的沙箱、先结束者 clear 抹掉别人上下文。
# ContextVar 在每个 asyncio task 有独立副本，参照 tools/scope_guard.py 的正确做法。
import contextvars

_sandbox_var: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "swarm_current_sandbox", default=None)
_sandbox_manager_var: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "swarm_current_sandbox_manager", default=None)
_extra_whitelist_var: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
    "swarm_extra_whitelist", default=[])


def set_extra_whitelist(prefixes: list[str] | None) -> None:
    """设置当前 worker 的额外命令白名单（harness.extra_whitelist）。"""
    _extra_whitelist_var.set(list(prefixes or []))


def get_extra_whitelist() -> list[str]:
    return list(_extra_whitelist_var.get())


def clear_extra_whitelist() -> None:
    _extra_whitelist_var.set([])


_worker_deadline_var: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "swarm_worker_deadline", default=None)


def set_worker_deadline(deadline: float | None) -> None:
    """C8（阶段4，登记册 §四）：登记当前 worker 的总预算 deadline（monotonic 绝对时刻）。

    WorkerExecutor 在 run() 起点设置、finally 清除；contextvars 随 to_thread/run_in_executor
    拷贝传播——agent 超时后成为孤儿的同步工具线程，其【下一次】工具调用在 _run 入口撞
    哨兵立即返回（不再对已销毁沙箱烧请求到自身超时），在飞调用的超时也被钳到剩余预算。"""
    _worker_deadline_var.set(deadline)


def get_worker_deadline() -> float | None:
    return _worker_deadline_var.get()


def clear_worker_deadline() -> None:
    _worker_deadline_var.set(None)


def set_sandbox_context(sandbox: Any, manager: Any) -> None:
    """设置沙箱上下文（WorkerExecutor 调用，按 asyncio task 隔离）"""
    _sandbox_var.set(sandbox)
    _sandbox_manager_var.set(manager)


def get_sandbox_context() -> tuple[Any | None, Any | None]:
    """获取当前沙箱上下文"""
    return _sandbox_var.get(), _sandbox_manager_var.get()


def clear_sandbox_context() -> None:
    """清除沙箱上下文"""
    _sandbox_var.set(None)
    _sandbox_manager_var.set(None)


def _is_command_allowed(command: str, whitelist: list[str]) -> tuple[bool, str]:
    """检查命令是否在白名单中

    匹配规则：命令以白名单中的某项作为前缀即为允许。
    例如白名单 'mvn test' 允许 'mvn test -DskipTests' 等。

    Returns:
        (allowed, matched_prefix_or_empty)
    """
    cmd_stripped = command.strip()
    for allowed in whitelist:
        if cmd_stripped == allowed or cmd_stripped.startswith(allowed + " "):
            return True, allowed
    return False, ""


# ── H7 修复：shell 元字符注入检测 ──
# 白名单前缀检查无法阻止 'mvn test; rm -rf ~' 这类拼接命令，
# 因为 '; rm -rf ~' 躲在前缀匹配之后。在执行前额外检测危险元字符。
_SHELL_INJECTION_RE = re.compile(
    r"""(?x)        # verbose mode
    ;               # command separator
    | \|            # pipe
    | &             # background / AND / OR
    | \$\(          # command substitution $(...)
    | `             # backtick command substitution
    | [><]          # redirection
    | \n            # newline (another command separator)
    """
)

_SAFE_CHARS_PATTERN = re.compile(r"""^[a-zA-Z0-9 ./_\-:=,.~@#%^+\[\]]+$""")


def _has_shell_injection(command: str) -> tuple[bool, str]:
    """检测命令中是否包含危险的 shell 元字符。

    Returns:
        (has_injection, reason) — has_injection 为 True 表示命令应被拒绝，
        reason 为命中的危险字符/模式描述。
    """
    # 快速路径：只含安全字符的命令无需正则扫描
    if _SAFE_CHARS_PATTERN.match(command):
        return False, ""

    # 逐项检测危险模式，返回第一个命中的描述（帮助诊断）
    for match in _SHELL_INJECTION_RE.finditer(command):
        ch = match.group()
        descriptions = {
            ";": "分号(命令分隔符)",
            "|": "管道符",
            "&": "与/后台符",
            "$(": "命令替换 $(...)",
            "`": "反引号命令替换",
            ">": "重定向 >",
            "<": "重定向 <",
            "\n": "换行(命令分隔符)",
        }
        return True, descriptions.get(ch, f"危险元字符 {ch!r}")
    return False, ""


def _run_local(command: str, cwd: Path | None = None, timeout: int = 120) -> str:
    """本地执行 shell 命令"""
    # P0-4：本地执行路径过去完全绕过命令黑名单（黑名单只在 sandbox.run_command 强制）。
    # 无沙箱时 worker 命令落宿主机 shell=True 执行、又无沙箱隔离兜底 → 必须在此统一拦截。
    # 用 hardened 入口（异常回退基线，#1(b) 同源），命中即拒绝执行、绝不落 subprocess。
    from swarm.config import command_blacklist_store
    _allowed, _reason = command_blacklist_store.check_command_hardened(command)
    if not _allowed:
        return f"❌ 命令被安全黑名单拦截（{_reason}）— 未执行"
    try:
        cfg = _worker_config()
        effective_timeout = min(timeout, cfg.max_execution_time)
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd or _workspace_root(),
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        output = output.strip()

        status = "✅" if result.returncode == 0 else "❌"
        return f"{status} (exit code {result.returncode})\n{output}"
    except subprocess.TimeoutExpired:
        return f"❌ 命令超时（{timeout}s）"
    except Exception as e:
        return f"❌ 执行失败：{e}"


def _sandbox_workdir() -> str:
    return get_config().sandbox.sandbox_remote_workdir


def _run_in_sandbox(command: str, timeout: int = 120) -> str:
    """在远程沙箱中执行 shell 命令"""
    import logging
    log = logging.getLogger(__name__)

    sandbox, manager = get_sandbox_context()
    if sandbox is None or manager is None:
        return "❌ 没有活跃的远程沙箱，请先创建沙箱"

    workdir = _sandbox_workdir()
    sandbox_command = f"cd {workdir} && {command}"

    # 优先用沙箱原生 shell 端点(commands.run)执行——所有语言镜像都可用，
    # 不依赖 Jupyter kernel(自建语言镜像未装 kernel 会 502)。
    if hasattr(manager, "run_command"):
        cr = manager.run_command(sandbox, sandbox_command, timeout=timeout)
        # 基础设施级失败(连接/502/没有 exit_code)才降级；命令非0退出是有效结果
        infra_fail = (not cr.success) and (cr.error or "").startswith(
            ("TimeoutException", "SandboxException", "ConnectionError", "502", "500")
        )
        if infra_fail:
            # P0-SEC-08：沙箱是隔离边界。沙箱已激活但单次基础设施失败时【绝不能】降级到
            # _run_local（worker 命令会落 swarm 宿主机 shell=True 执行，越过隔离边界）。
            # fail-closed：返回错误交由 executor 瞬时重试，命令不在宿主机执行。
            log.error("沙箱 shell 端点基础设施失败，fail-closed 拒绝执行（不落宿主机）: %s", cr.error)
            return f"❌ 沙箱不可用(基础设施失败)，命令未执行(fail-closed 隔离边界): {cr.error}"
        status = "✅" if cr.success else "❌"
        exit_hint = "0" if cr.success else (cr.error or "non-zero")
        body = cr.stdout + (("\n" + cr.stderr) if cr.stderr else "")
        # 防上下文爆炸：压缩超长命令输出(mvn/npm 可输出上万行)，保留关键失败信号。
        try:
            from swarm.worker.output_compress import compress_tool_output
            body = compress_tool_output(body.strip(), max_chars=4000)
        except Exception:
            body = body.strip()[:4000]
        return f"{status} (sandbox {exit_hint})\n{body}"

    # 兜底(旧路径)：manager 无 run_command 时用 Jupyter 包 subprocess。
    # R2-3：此路径承载的是【agent 生成的命令】，必须过与 run_command 同一黑名单——
    # 否则该兜底成为绕过口（实践中 SandboxManager 恒有 run_command 不可达，但边界不靠巧合）。
    from swarm.config import command_blacklist_store as _cbs
    _allowed, _reason = _cbs.check_command_hardened(sandbox_command)
    if not _allowed:
        return f"⛔ 命令被安全黑名单拦截：{_reason}"
    code = f"""
import subprocess
_sbx_proc = subprocess.run({sandbox_command!r}, shell=True, capture_output=True, text=True, timeout={timeout})
print(_sbx_proc.stdout)
if _sbx_proc.stderr:
    print(_sbx_proc.stderr, end='')
print(f"EXIT_CODE:{{_sbx_proc.returncode}}")
"""
    code_result = manager.run_code(sandbox, code, timeout=timeout + 10)

    if code_result.error:
        # P0-SEC-08：同上，沙箱激活下基础设施失败 fail-closed，不落宿主机执行。
        log.error("沙箱命令基础设施失败，fail-closed 拒绝执行（不落宿主机）: %s", code_result.error)
        return f"❌ 沙箱不可用(基础设施失败)，命令未执行(fail-closed 隔离边界): {code_result.error}"

    # 解析输出
    output = code_result.stdout
    exit_code = 0
    if "EXIT_CODE:" in output:
        parts = output.rsplit("EXIT_CODE:", 1)
        output = parts[0].strip()
        try:
            exit_code = int(parts[1].strip().split()[0])
        except (ValueError, IndexError):
            pass

    status = "✅" if exit_code == 0 else "❌"
    return f"{status} (sandbox exit code {exit_code})\n{output}"


def _clamp_to_worker_deadline(timeout: int) -> tuple[int, bool]:
    """C8：工具超时钳到 worker 剩余预算。返回 (有效超时, 预算已尽)。"""
    dl = _worker_deadline_var.get()
    if dl is None:
        return timeout, False
    import time as _t
    remaining = dl - _t.monotonic()
    if remaining <= 1:
        return 0, True
    return max(5, min(int(timeout), int(remaining))), False


def _run(command: str, cwd: Path | None = None, timeout: int = 120) -> str:
    """智能选择执行模式：有沙箱用沙箱，否则本地执行"""
    # C8（阶段4）：收尾哨兵+超时钳——worker 预算耗尽后，孤儿工具线程（agent 已被
    # wait_for 取消，同步线程杀不死）的后续命令在此立即返回，不再对已销毁沙箱烧
    # 请求到自身超时；未耗尽时工具超时钳到剩余预算（不冲破 worker deadline）。
    timeout, _exhausted = _clamp_to_worker_deadline(timeout)
    if _exhausted:
        return "❌ worker 预算已耗尽，命令未执行（收尾哨兵拦截）"
    sandbox, _ = get_sandbox_context()
    if sandbox is not None:
        return _run_in_sandbox(command, timeout)
    return _run_local(command, cwd, timeout)


# ── P1-C：命令规范化/守卫（减少本地小模型烧预算的无效命令）──
_MVN_LIFECYCLE = {
    "validate", "initialize", "generate-sources", "process-sources", "compile",
    "process-classes", "test-compile", "test", "package", "verify", "install",
    "deploy", "clean", "site", "integration-test", "prepare-package",
}


def _strip_cd_prefix(command: str) -> tuple[str, str]:
    """剥掉 `cd X && `/`cd X ; ` 前缀 → (前缀含分隔符, 剩余命令)。

    round48c 实锤（M1）：Qwopus 习惯性给命令加 `cd /workspace && `，规范化/守卫都
    只看首 token → mvn 误用改写 0 拦截（54 次白烧）、git 守卫 0 拦截（39 次必败）。
    只剥【单个简单 cd 段】（无嵌套引号/子 shell），剥不动原样返回（保守）。
    """
    m = re.match(r"^(\s*cd\s+[^;&|()\"']+(?:&&|;)\s*)(.+)$", command, re.S)
    if m:
        return m.group(1), m.group(2)
    return "", command


def _normalize_maven_module_command(command: str) -> tuple[str, bool]:
    """把模型常犯的 `mvn compile <module>`（模块名当生命周期阶段，必报 Unknown lifecycle
    phase）静默改写为正确的 `mvn -pl <module> -am compile`，省掉一轮白跑。

    仅在【明确是该误用形态】（mvn + 生命周期阶段 + 恰一个裸模块名、且未含 -pl/-f）时改写，
    否则原样返回，绝不误改正常命令。返回 (命令, 是否改写)。
    M1：先剥 `cd X && ` 前缀再判形态（改写后前缀原样拼回）。
    """
    _pfx, command = _strip_cd_prefix(command)
    parts = command.strip().split()
    if len(parts) < 3 or parts[0] != "mvn":
        return _pfx + command, False
    if any(p in ("-pl", "-f", "--projects", "--file", "-N") for p in parts):
        return _pfx + command, False
    args = parts[1:]
    phases = [p for p in args if p in _MVN_LIFECYCLE]
    bare = [
        p for p in args
        if not p.startswith("-") and "=" not in p and ":" not in p
        and "/" not in p and p not in _MVN_LIFECYCLE
    ]
    if phases and len(bare) == 1:
        mod = bare[0]
        rest = [p for p in args if p != mod]
        return _pfx + "mvn -pl " + mod + " -am " + " ".join(rest), True
    return _pfx + command, False


def _guard_unhelpful_command(command: str) -> str | None:
    """拦在沙箱里注定白烧步数的命令，返回一行提示（不执行）；无需拦截返回 None。

    git 查看类：烤源沙箱（项目专属镜像）常【不含 .git】，git 必 128/129 失败；且沙箱内改动
    由确定性 L1 闸门自动追踪，worker 本就无需用 git。拦掉省得模型反复撞失败。
    """
    _, command = _strip_cd_prefix(command)  # M1：cd 前缀不豁免守卫
    if re.match(r"\s*git\s+(diff|log|status|show|blame|ls-files|rev-parse|stash)\b", command):
        return (
            "ℹ️ 沙箱内改动由确定性 L1 闸门自动追踪，无需也勿用 git 查看"
            "（烤源沙箱常无 .git，git 命令会失败）。请直接读写文件，编译/验证交闸门。"
        )
    return None


@tool
def run_command(command: str, timeout: int = 120) -> str:
    """执行白名单内的 shell 命令。

    仅允许配置中 command_whitelist 列表内的命令前缀。
    例如 'mvn compile'、'python -m pytest' 等。

    Args:
        command: 要执行的完整命令
        timeout: 超时秒数，默认 120

    Returns:
        命令输出或权限拒绝消息
    """
    cfg = _worker_config()
    effective_whitelist = list(cfg.command_whitelist) + get_extra_whitelist()
    allowed, matched = _is_command_allowed(command, effective_whitelist)

    if not allowed:
        whitelist_str = "\n  - ".join(effective_whitelist)
        return (
            f"⛔ 命令被拒绝：'{command}' 不在白名单中。\n"
            f"允许的命令前缀：\n  - {whitelist_str}\n"
            f"如需执行该命令，请联系管理员将其加入 SWARM_WORKER_COMMAND_WHITELIST。"
        )

    # H7 修复：白名单通过后，检测 shell 元字符注入
    has_injection, reason = _has_shell_injection(command)
    if has_injection:
        return f"⛔ 命令被拒绝：'{command}' 包含{reason}，可能存在 shell 注入风险。"

    # P1-C：堵住本地小模型烧预算的两类无效命令（996db614 实测白烧几十步 → 喂大 900s 超时）。
    guard = _guard_unhelpful_command(command)
    if guard is not None:
        return guard
    # mvn 模块语法误用静默改写为正确 -pl 形式，省掉"Unknown lifecycle phase"白跑一轮。
    command, _normalized = _normalize_maven_module_command(command)

    return _run(command, timeout=timeout)


@tool
def run_compile(language: str = "auto", target: str = "") -> str:
    """执行编译命令（白名单制）。自动根据语言选择编译命令。

    Args:
        language: 编程语言，支持 'java'/'maven'、'typescript'/'ts'、'python'、'auto'（自动检测）
        target: 编译目标（如特定模块路径），默认编译整个项目

    Returns:
        编译输出或错误消息
    """
    cfg = _worker_config()

    # 语言 → 命令映射
    compile_commands = {
        "java": "mvn compile",
        "maven": "mvn compile",
        "typescript": "tsc --noEmit",
        "ts": "tsc --noEmit",
        "python": "python -m py_compile",
        "javac": "javac",
    }

    if language == "auto":
        # 基于 whitelist 有哪些来推断
        whitelist = cfg.command_whitelist
        if any("mvn compile" in w for w in whitelist):
            cmd = "mvn compile"
        elif any("javac" in w for w in whitelist):
            cmd = "javac"
        elif any("tsc" in w for w in whitelist):
            cmd = "tsc --noEmit"
        elif any("python" in w for w in whitelist):
            cmd = "python -m py_compile"
        else:
            return "❌ 无法自动检测编译命令，请指定 language 参数"
    else:
        cmd = compile_commands.get(language.lower())
        if cmd is None:
            return f"❌ 不支持的语言：{language}，支持：{list(compile_commands.keys())}"

    if target:
        cmd = f"{cmd} {target}"

    # A8 治本：与 run_command 对齐——并入 extra_whitelist（harness 下发的构建命令前缀），
    # 否则 harness 合法命令被拒 → agent 自验失败空烧 fix 轮。
    effective_whitelist = list(cfg.command_whitelist) + get_extra_whitelist()
    allowed, matched = _is_command_allowed(cmd, effective_whitelist)
    if not allowed:
        whitelist_str = "\n  - ".join(effective_whitelist)
        return (
            f"⛔ 编译命令被拒绝：'{cmd}' 不在白名单中。\n"
            f"允许的命令前缀：\n  - {whitelist_str}"
        )

    # H7 修复：白名单通过后，检测 shell 元字符注入
    has_injection, reason = _has_shell_injection(cmd)
    if has_injection:
        return f"⛔ 编译命令被拒绝：'{cmd}' 包含{reason}，可能存在 shell 注入风险。"

    return _run(cmd, timeout=180)


@tool
def run_tests(
    test_filter: str = "",
    language: str = "auto",
    timeout: int = 180,
) -> str:
    """执行测试命令（白名单制）。

    Args:
        test_filter: 测试过滤模式（如测试类名或文件路径），默认运行所有测试
        language: 编程语言，用于选择测试框架，默认自动检测
        timeout: 超时秒数，默认 180

    Returns:
        测试输出或错误消息
    """
    cfg = _worker_config()

    # 语言 → 测试命令映射
    test_commands = {
        "java": "mvn test",
        "maven": "mvn test",
        "typescript": "npm test",
        "ts": "npm test",
        "python": "python -m pytest",
        "jest": "npm test",
        "pytest": "python -m pytest",
    }

    if language == "auto":
        # A8：auto 检测也并入 extra_whitelist，否则 harness-only 的构建命令检测不到。
        whitelist = list(cfg.command_whitelist) + get_extra_whitelist()
        if any("mvn test" in w for w in whitelist):
            cmd = "mvn test"
        elif any("npm test" in w for w in whitelist):
            cmd = "npm test"
        elif any("pytest" in w for w in whitelist):
            cmd = "python -m pytest"
        else:
            return "❌ 无法自动检测测试命令，请指定 language 参数"
    else:
        cmd = test_commands.get(language.lower())
        if cmd is None:
            return f"❌ 不支持的语言：{language}，支持：{list(test_commands.keys())}"

    if test_filter:
        cmd = f"{cmd} {test_filter}"

    # A8 治本：与 run_command 对齐——并入 extra_whitelist。
    effective_whitelist = list(cfg.command_whitelist) + get_extra_whitelist()
    allowed, matched = _is_command_allowed(cmd, effective_whitelist)
    if not allowed:
        whitelist_str = "\n  - ".join(effective_whitelist)
        return (
            f"⛔ 测试命令被拒绝：'{cmd}' 不在白名单中。\n"
            f"允许的命令前缀：\n  - {whitelist_str}"
        )

    # H7 修复：白名单通过后，检测 shell 元字符注入
    has_injection, reason = _has_shell_injection(cmd)
    if has_injection:
        return f"⛔ 测试命令被拒绝：'{cmd}' 包含{reason}，可能存在 shell 注入风险。"

    return _run(cmd, timeout=timeout)
