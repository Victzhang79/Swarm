"""task 16098179 回归：MERGE 输出 diff 行尾翻倍（\\n\\n）→ git apply 补丁损坏。

根因：worker 产出的 diff 行尾为 \\n\\n（双换行）时，_parse_file_patch 的
raw.splitlines() 会在 hunk.lines 里留下纯空字符串元素，_format_file_patch
用 "\\n".join 拼接后这些空行变成额外换行 → 整段 diff 行尾翻倍 →
git apply 看到 hunk 实际行数 != @@ 声明行数 → "补丁损坏"。
修复：_format_file_patch / _format_conflict_hunks 过滤 body 内的纯空行（""），
保留真实空 context 行（" " 单空格）。
"""
import os
import subprocess
import tempfile

from swarm.brain.merge_engine import merge_diffs


def _git_apply_check(merged_diff: str, files: dict[str, str]) -> tuple[int, str]:
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    for name, content in files.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(content)
    pf = os.path.join(d, "p.diff")
    with open(pf, "w") as f:
        f.write(merged_diff if merged_diff.endswith("\n") else merged_diff + "\n")
    res = subprocess.run(
        ["git", "apply", "--check", pf], cwd=d, capture_output=True, text=True, check=False
    )
    return res.returncode, res.stderr


def test_merge_normal_diff_not_corrupted():
    """正常单换行 diff 合并后能被 git apply 解析。"""
    normal = "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,4 @@\n line1\n line2\n line3\n+new line\n"
    r = merge_diffs([("st-1", normal)])
    assert r.success
    # 内部不应有双换行（行尾翻倍）
    assert "\n\n" not in r.merged_diff.rstrip("\n")
    rc, err = _git_apply_check(r.merged_diff, {"A.java": "line1\nline2\nline3\n"})
    assert rc == 0, f"git apply 失败: {err}"


def test_merge_double_newline_diff_repaired():
    """worker 产出行尾 \\n\\n 的坏 diff，合并时应被修复为干净 diff。"""
    bad = (
        "--- a/B.java\n+++ b/B.java\n@@ -1,3 +1,4 @@\n\n line1\n\n line2\n\n line3\n\n+new line\n\n"
    )
    r = merge_diffs([("st-1", bad)])
    assert r.success
    # 关键：修复后内部不再有双换行
    assert "\n\n" not in r.merged_diff.rstrip("\n"), f"仍有行尾翻倍: {r.merged_diff!r}"
    rc, err = _git_apply_check(r.merged_diff, {"B.java": "line1\nline2\nline3\n"})
    assert rc == 0, f"修复后 git apply 仍失败: {err}"


def test_merge_preserves_real_blank_context_line():
    """真实空 context 行（diff 里的 ' ' 单空格）不被误删。"""
    # A.java 第 2 行是空行
    diff = (
        "--- a/A.java\n+++ b/A.java\n@@ -1,4 +1,5 @@\n class A {\n \n"
        "     void f() {}\n+    void g() {}\n }\n"
    )
    r = merge_diffs([("st-1", diff)])
    assert r.success
    rc, err = _git_apply_check(r.merged_diff, {"A.java": "class A {\n\n    void f() {}\n}\n"})
    assert rc == 0, f"空 context 行被误删导致 apply 失败: {err}"


def test_merge_two_different_files_clean():
    """两个子任务改不同文件（task 16098179 场景）合并后干净可 apply。"""
    diff1 = "--- a/X.java\n+++ b/X.java\n@@ -1,2 +1,3 @@\n a\n b\n+c\n"
    diff2 = "--- a/Y.java\n+++ b/Y.java\n@@ -1,2 +1,3 @@\n p\n q\n+r\n"
    r = merge_diffs([("st-1", diff1), ("st-2", diff2)])
    assert r.success
    assert len(r.conflicts) == 0
    assert "\n\n\n" not in r.merged_diff  # 文件间最多一个空行分隔
    rc, err = _git_apply_check(r.merged_diff, {"X.java": "a\nb\n", "Y.java": "p\nq\n"})
    assert rc == 0, f"双文件合并 apply 失败: {err}"
