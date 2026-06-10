"""构建与测试 Tool 集 — run_command / run_compile / run_tests

支持两种执行模式：
  1. 本地模式（默认）— subprocess 执行
  2. 远程沙箱模式 — 在 CubeSandbox (E2B) 中执行

run_command 使用白名单制，不在白名单中的命令会被拒绝。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import tool

from swarm.config.settings import WorkerConfig, get_config


def _workspace_root() -> Path:
    from swarm.tools.paths import workspace_root
    return workspace_root()


def _worker_config() -> WorkerConfig:
    return get_config().worker


# ── 远程沙箱上下文 ──
_current_sandbox: Any | None = None
_current_sandbox_manager: Any | None = None


def set_sandbox_context(sandbox: Any, manager: Any) -> None:
    """设置全局沙箱上下文（WorkerExecutor 调用）"""
    global _current_sandbox, _current_sandbox_manager
    _current_sandbox = sandbox
    _current_sandbox_manager = manager


def get_sandbox_context() -> tuple[Any | None, Any | None]:
    """获取当前沙箱上下文"""
    return _current_sandbox, _current_sandbox_manager


def clear_sandbox_context() -> None:
    """清除沙箱上下文"""
    global _current_sandbox, _current_sandbox_manager
    _current_sandbox = None
    _current_sandbox_manager = None


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


def _run_local(command: str, cwd: Path | None = None, timeout: int = 120) -> str:
    """本地执行 shell 命令"""
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

    # 使用 subprocess 模块在沙箱内执行 shell 命令
    # 注意：变量名用 _sbx_proc 避免与外层 result 冲突
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
        log.warning("沙箱命令失败，降级本地执行: %s", code_result.error)
        return _run_local(command, timeout=timeout)

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


def _run(command: str, cwd: Path | None = None, timeout: int = 120) -> str:
    """智能选择执行模式：有沙箱用沙箱，否则本地执行"""
    sandbox, _ = get_sandbox_context()
    if sandbox is not None:
        return _run_in_sandbox(command, timeout)
    return _run_local(command, cwd, timeout)


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
    allowed, matched = _is_command_allowed(command, cfg.command_whitelist)

    if not allowed:
        whitelist_str = "\n  - ".join(cfg.command_whitelist)
        return (
            f"⛔ 命令被拒绝：'{command}' 不在白名单中。\n"
            f"允许的命令前缀：\n  - {whitelist_str}\n"
            f"如需执行该命令，请联系管理员将其加入 SWARM_WORKER_COMMAND_WHITELIST。"
        )

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

    allowed, matched = _is_command_allowed(cmd, cfg.command_whitelist)
    if not allowed:
        whitelist_str = "\n  - ".join(cfg.command_whitelist)
        return (
            f"⛔ 编译命令被拒绝：'{cmd}' 不在白名单中。\n"
            f"允许的命令前缀：\n  - {whitelist_str}"
        )

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
        whitelist = cfg.command_whitelist
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

    allowed, matched = _is_command_allowed(cmd, cfg.command_whitelist)
    if not allowed:
        whitelist_str = "\n  - ".join(cfg.command_whitelist)
        return (
            f"⛔ 测试命令被拒绝：'{cmd}' 不在白名单中。\n"
            f"允许的命令前缀：\n  - {whitelist_str}"
        )

    return _run(cmd, timeout=timeout)
