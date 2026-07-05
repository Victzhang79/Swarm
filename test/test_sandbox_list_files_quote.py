"""P0-3（CODEWALK_AUDIT_2026-07-06 批1）：list_files shell 兜底用 Python repr 拼路径。

原 bug：worker/sandbox.py list_files 的 run_command 兜底 f"cd {path!r} && ls ..."——
Python repr 不是 shell 转义（含单引号的路径 repr 换双引号包裹，shell 里 $/反引号仍会
展开与执行）→ 引号错配/命令注入面。同文件下载路径早已用 shlex.quote（P0-SEC-05(b)），
本处为 round25 shlex 全仓扫漏掉的 sibling 漂移。
修复：shlex.quote(path)。（run_code 兜底里的 {path!r} 是拼 Python 源码，repr 正确，不动。）
"""
from __future__ import annotations

import shlex

from swarm.worker.sandbox import SandboxManager


class _Result:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""


def _bare_manager() -> SandboxManager:
    # 绕过 __init__（_setup_env/_init_sidecar 有网络副作用），list_files 只用到 _instances
    m = object.__new__(SandboxManager)
    m._instances = {"sb-1": object()}  # 无 .files 属性 → 直落 shell 兜底
    return m


def test_list_files_shell_fallback_quotes_path():
    m = _bare_manager()
    captured: dict = {}

    def _fake_run_command(sandbox, command, timeout=120, **kw):
        captured["cmd"] = command
        return _Result("total 0\n-rw-r--r-- 1 u 12 file.txt")

    m.run_command = _fake_run_command
    path = "/workspace/o'dir; rm -rf ~/$(hostname)"
    files = m.list_files("sb-1", path)

    cmd = captured["cmd"]
    assert cmd.startswith(f"cd {shlex.quote(path)} "), f"路径必须 shlex.quote 转义: {cmd}"
    assert repr(path) not in cmd, f"不得用 Python repr 拼 shell: {cmd}"
    # 解析路径不受影响（拼 abs_path 用原始 path）
    assert any(f["name"] == "file.txt" for f in files), files


def test_list_files_plain_path_still_works():
    """对照：普通路径行为不变。"""
    m = _bare_manager()
    captured: dict = {}

    def _fake_run_command(sandbox, command, timeout=120, **kw):
        captured["cmd"] = command
        return _Result("total 0\n-rw-r--r-- 1 u 34 App.java\ndrwxr-xr-x 2 u 0 src/")

    m.run_command = _fake_run_command
    files = m.list_files("sb-1", "/workspace/proj")

    names = {f["name"]: f for f in files}
    assert "App.java" in names and not names["App.java"]["is_dir"]
    assert "src" in names and names["src"]["is_dir"]
    assert names["App.java"]["path"] == "/workspace/proj/App.java"
