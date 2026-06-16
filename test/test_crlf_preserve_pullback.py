"""task f20ea68d 回归：CRLF 项目 pull-back 写回保留行尾 → git diff/apply 同源。

根因：RuoYi 等项目源文件 CRLF，worker 沙箱改文件变 LF，直接 write_bytes 覆盖使工作区
变 LF → 与 git HEAD(CRLF) 行尾不一致 → git diff churn 或 LF context 无法 apply 回 CRLF。
修复：_preserve_line_endings 在写回时把 LF 内容转回本地原文件的 CRLF。
"""
import os
import subprocess
import tempfile

from swarm.worker.sandbox import SandboxManager
from pathlib import Path


def test_preserve_crlf_when_local_is_crlf():
    """本地 CRLF + 沙箱返回 LF → 转回 CRLF。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "F.java"
    p.write_bytes(b"line1\r\nline2\r\nline3\r\n")  # 本地 CRLF
    new_lf = b"line1\nline2\nNEW\nline3\n"  # 沙箱返回 LF（加了一行）
    out = SandboxManager._preserve_line_endings(p, new_lf)
    assert out == b"line1\r\nline2\r\nNEW\r\nline3\r\n", repr(out)


def test_keep_lf_when_local_is_lf():
    """本地 LF → 不动。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "F.py"
    p.write_bytes(b"a\nb\nc\n")
    new = b"a\nb\nX\nc\n"
    out = SandboxManager._preserve_line_endings(p, new)
    assert out == new


def test_new_file_not_converted():
    """本地不存在（新建）→ 按沙箱产出，不转。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "New.java"
    new = b"x\ny\n"
    out = SandboxManager._preserve_line_endings(p, new)
    assert out == new


def test_binary_not_touched():
    """二进制（含 NUL）→ 不动。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "b.bin"
    p.write_bytes(b"\x00\x01\r\n\x00")
    new = b"\x00\x02\n\x00"
    out = SandboxManager._preserve_line_endings(p, new)
    assert out == new


def test_idempotent_crlf_input():
    """沙箱已返回 CRLF（少见）→ 保持 CRLF，不产生 \\r\\r\\n。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "F.java"
    p.write_bytes(b"a\r\nb\r\n")
    new_crlf = b"a\r\nb\r\nc\r\n"
    out = SandboxManager._preserve_line_endings(p, new_crlf)
    assert out == b"a\r\nb\r\nc\r\n"
    assert b"\r\r" not in out


def test_e2e_crlf_diff_applies():
    """端到端：CRLF 文件 + LF 改动经保留后，bytes 模式 git diff 产 CRLF context，git apply 成功。"""
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    f = Path(d) / "S.java"
    # HEAD: CRLF
    f.write_bytes(b"class S {\r\n\r\n    void f() {}\r\n}\r\n")
    subprocess.run(["git", "-C", d, "add", "-A"], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        capture_output=True, check=False,
    )
    # 模拟 worker 沙箱改文件（LF）+ pull-back 保留行尾
    new_lf = b"class S {\n\n    void f() {}\n    void g() {}\n}\n"
    preserved = SandboxManager._preserve_line_endings(f, new_lf)
    f.write_bytes(preserved)
    # git diff —— 关键：bytes 模式（不传 text=True），保留 \r
    diff_bytes = subprocess.run(
        ["git", "-C", d, "diff", "--no-color", "--", "S.java"],
        capture_output=True, check=False,
    ).stdout
    # context 行应带 \r（CRLF 同源）
    assert b"\r" in diff_bytes, f"diff 应保留 CRLF 的 \\r: {diff_bytes!r}"
    # reset 回 HEAD，apply diff
    subprocess.run(["git", "-C", d, "checkout", "--", "S.java"], capture_output=True, check=False)
    pf = Path(d) / "p.diff"
    pf.write_bytes(diff_bytes)
    res = subprocess.run(
        ["git", "-C", d, "apply", "--check", str(pf)], cwd=d, capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, f"CRLF 同源 diff 应能 apply: {res.stderr}\ndiff={diff_bytes!r}"
