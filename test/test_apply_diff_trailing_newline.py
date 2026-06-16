"""task bce82e96 回归：apply_git_diff 写 patch 文件必须补尾部换行 + bytes 模式保 CRLF。

根因：apply_git_diff 用 mode="w" 文本写临时 patch 且不补尾换行。worker git diff 经
rstrip("\\n") 后末尾无换行 → git apply 把最后一行 hunk 判 "corrupt patch at line N"
（末行截断）→ VERIFY_L2 integration_review 永远失败、medium 路径卡死 replan。
注意：diff 本身用 git CLI apply 是通过的；bug 在 apply 端写文件方式（末行缺 \\n + 文本模式）。
修复：encode bytes 写 + 末尾无 \\n 则补 \\n。
"""
import os
import subprocess
import tempfile

from swarm.project.diff_apply import apply_git_diff


def _init_repo(files: dict[str, bytes]) -> str:
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
    return d


def test_apply_diff_without_trailing_newline():
    """末行无换行的 diff（worker rstrip 后的典型形态）必须能 apply。"""
    d = _init_repo({"A.java": b"class A {\n    int x;\n}\n"})
    # 末尾【没有】换行符的 diff
    diff = "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,4 @@\n class A {\n+    int z;\n     int x;\n }"
    assert not diff.endswith("\n")
    res = apply_git_diff(d, diff, check_only=True)
    assert res.get("ok"), f"末行无换行的 diff 应能 apply: {res.get('stderr')}"


def test_apply_crlf_diff_no_trailing_newline():
    """CRLF 项目 + 末行无换行：bytes 写保 \\r + 补换行，git apply 成功。"""
    d = _init_repo({"S.java": b"class S {\r\n    int x;\r\n}\r\n"})
    # CRLF context + 末行无最终换行
    diff = "--- a/S.java\r\n+++ b/S.java\r\n@@ -1,3 +1,4 @@\r\n class S {\r\n+    int z;\r\n     int x;\r\n }"
    assert not diff.endswith("\n")
    res = apply_git_diff(d, diff, check_only=True)
    assert res.get("ok"), f"CRLF+末行无换行的 diff 应能 apply: {res.get('stderr')}"
