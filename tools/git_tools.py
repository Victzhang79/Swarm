"""Git 操作 Tool 集 — checkout / diff / log / blame

所有 Tool 自动检查 FileScope 权限（可读范围）。
Worker 启用沙箱时，git 命令在沙箱 /workspace 内执行。
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from swarm.tools.scope_guard import require_readable


def _workspace_root() -> Path:
    from swarm.tools.paths import workspace_root
    return workspace_root()


def _run_git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """执行 git 命令，返回 (returncode, stdout+stderr)"""
    from swarm.tools.build_tools import _run_in_sandbox, get_sandbox_context

    sandbox, _ = get_sandbox_context()
    if sandbox is not None:
        cmd = "git " + " ".join(shlex.quote(a) for a in args)
        raw = _run_in_sandbox(cmd, timeout=60)
        rc = 0 if "sandbox exit code 0" in raw else 1
        if "\n" in raw:
            output = raw.split("\n", 1)[1]
        else:
            output = ""
        return rc, output.strip()

    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd or _workspace_root(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        return result.returncode, output.strip()
    except FileNotFoundError:
        return 1, "❌ git 命令未找到，请确认已安装 git"
    except subprocess.TimeoutExpired:
        return 1, "❌ git 命令超时（60s）"
    except Exception as e:
        return 1, f"❌ git 执行失败：{e}"


@tool
def git_checkout(branch: str, create: bool = False) -> str:
    """切换 git 分支。

    Args:
        branch: 目标分支名
        create: 是否创建新分支，默认 False

    Returns:
        命令输出或错误消息
    """
    args = ["checkout"]
    if create:
        args.append("-b")
    args.append(branch)

    rc, output = _run_git(args)
    if rc == 0:
        return f"✅ 已切换到分支 {branch}\n{output}"
    return f"❌ 切换分支失败：{output}"


@tool
def git_diff(
    target: str = "HEAD",
    path: str = "",
    staged: bool = False,
) -> str:
    """查看 git diff。

    Args:
        target: 对比目标，默认 HEAD（即查看工作区变更）。如 'main' 对比 main 分支
        path: 限制到特定文件或目录路径
        staged: 是否只显示已暂存的变更，默认 False

    Returns:
        diff 输出或权限拒绝/错误消息
    """
    if path:
        err = require_readable(path)
        if err:
            return err

    args = ["diff"]
    if staged:
        args.append("--cached")
    if target:
        args.append(target)
    if path:
        args.extend(["--", path])

    rc, output = _run_git(args)
    if rc == 0:
        return output or "(无变更)"
    # 沙箱 /workspace 不是 git 仓库、或镜像无 git（git: not found / not a git
    # repository）。系统已用 difflib 独立采集权威 diff，这里无需 git，给出明确提示
    # 让 LLM 不要纠结于 git_diff，直接基于自己的改动描述变更即可。
    low = output.lower()
    if "not found" in low or "not a git repository" in low or "command not found" in low:
        return (
            "(沙箱非 git 仓库，git_diff 不可用。无需 git——系统会自动采集你的文件"
            "变更。请直接根据你刚才用 write_file/patch_file 做的改动撰写变更摘要。)"
        )
    return f"❌ git diff 失败：{output}"


@tool
def git_log(
    max_count: int = 20,
    path: str = "",
    author: str = "",
    oneline: bool = True,
) -> str:
    """查看 git 提交日志。

    Args:
        max_count: 最大提交数，默认 20
        path: 限制到特定文件路径
        author: 按作者筛选
        oneline: 是否单行显示，默认 True

    Returns:
        日志输出或权限拒绝/错误消息
    """
    if path:
        err = require_readable(path)
        if err:
            return err

    args = ["log"]
    if oneline:
        args.append("--oneline")
    args.extend(["-n", str(max_count)])
    if author:
        args.extend(["--author", author])
    if path:
        args.extend(["--", path])

    rc, output = _run_git(args)
    if rc == 0:
        return output or "(无提交)"
    return f"❌ git log 失败：{output}"


@tool
def git_blame(path: str, start_line: int = 1, end_line: int = -1) -> str:
    """查看文件每行的 git blame 信息（最后修改者+提交）。

    Args:
        path: 文件路径
        start_line: 起始行号（1-indexed），默认 1
        end_line: 结束行号，-1 表示文件末尾

    Returns:
        blame 输出或权限拒绝/错误消息
    """
    err = require_readable(path)
    if err:
        return err

    args = ["blame", "-l"]
    if start_line > 1 or end_line != -1:
        args.extend(["-L", f"{start_line},{end_line}"])
    args.append(path)

    rc, output = _run_git(args)
    if rc == 0:
        return output or "(空结果)"
    return f"❌ git blame 失败：{output}"
