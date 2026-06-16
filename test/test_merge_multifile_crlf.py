"""task 4b244174 回归：多文件 CRLF diff 经 MERGE 后必须保留每行 \\r → git apply 同源。

根因：_split_raw_diffs 用 .strip()（含 \\r 和尾部空格）切分文件块，吃掉了每个文件块
【最后一行 context】的 \\r → CRLF 项目的 merged_diff 末行 context 行尾不匹配 HEAD(CRLF)
→ git apply "补丁损坏/未应用"（medium 路径 VERIFY_L2 卡死）。
修复：_split_raw_diffs 只 strip("\\n")（保 \\r 和尾部空格）+ split("\\n") 替代 splitlines()。
"""
import os
import subprocess
import tempfile

from swarm.brain.merge_engine import merge_diffs, _split_raw_diffs


def _git_apply_check(merged_diff: str, files: dict[str, bytes]) -> tuple[int, str]:
    """在临时 git repo 用【字节】写文件+diff，git apply --check。"""
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    for name, content in files.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p) or d, exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
    subprocess.run(["git", "-C", d, "add", "-A"], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        capture_output=True, check=False,
    )
    pf = os.path.join(d, "p.diff")
    with open(pf, "wb") as f:
        data = merged_diff if merged_diff.endswith("\n") else merged_diff + "\n"
        f.write(data.encode("utf-8"))
    res = subprocess.run(
        ["git", "-C", d, "apply", "--check", pf], cwd=d, capture_output=True, text=True, check=False
    )
    return res.returncode, res.stderr


def test_split_raw_diffs_preserves_cr():
    """_split_raw_diffs 不得吃掉文件块末行的 \\r。"""
    diff = (
        "--- a/A.java\r\n+++ b/A.java\r\n@@ -1,2 +1,3 @@\r\n a\r\n b\r\n+c\r\n"
        "--- a/B.java\r\n+++ b/B.java\r\n@@ -1,2 +1,3 @@\r\n p\r\n q\r\n+r\r"
    )
    chunks = _split_raw_diffs(diff)
    assert len(chunks) == 2
    # 第一块末行（+c）和第二块末行（+r）的 \r 必须保留
    assert chunks[0].rstrip("\n").endswith("\r") or "\r" in chunks[0]
    assert "\r" in chunks[1]


def test_merge_two_crlf_files_git_apply():
    """两个 CRLF 文件的 diff 经 MERGE 后 git apply 成功（核心回归）。"""
    # 文件 HEAD：CRLF
    a = b"class A {\r\n    int x;\r\n}\r\n"
    b = b"class B {\r\n    int y;\r\n}\r\n"
    # 各加一行（CRLF），末行 context 是 } 带 \r
    d1 = "--- a/A.java\r\n+++ b/A.java\r\n@@ -1,3 +1,4 @@\r\n class A {\r\n+    int z;\r\n     int x;\r\n }\r\n"
    d2 = "--- a/B.java\r\n+++ b/B.java\r\n@@ -1,3 +1,4 @@\r\n class B {\r\n+    int w;\r\n     int y;\r\n }\r\n"
    r = merge_diffs([("st-1", d1 + d2)])
    assert r.success
    assert len(r.conflicts) == 0
    assert "\r" in r.merged_diff, "merged_diff 必须保留 CRLF 的 \\r"
    rc, err = _git_apply_check(r.merged_diff, {"A.java": a, "B.java": b})
    assert rc == 0, f"两 CRLF 文件 MERGE 后 git apply 应成功: {err}\ndiff={r.merged_diff!r}"
