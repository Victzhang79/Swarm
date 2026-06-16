"""MERGE 输出 diff 空 context 行处理 → git apply 必须接受。

历史(task 16098179)：worker difflib 产出 \\n\\n 行尾翻倍 → 旧逻辑删空行。
现状(task 3adfeca5)：worker 改用本地 git diff(同源,无翻倍)，真问题变成
"原文件空行的 context( ' ' 单空格)经传输被 strip 成 '' → 被误删 → hunk 行数对不上"。
修复：_format_file_patch 把 '' 还原为 ' '(单空格 context 标记)，保 hunk 行数。
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


def test_merge_stripped_blank_context_restored():
    """git diff 的空 context 行(" ")经传输被 strip 成 "" → 合并时应还原为 " "(保 hunk 行数)。

    task 3adfeca5 实测：worker git diff 含原文件空行的 context(表示为 " " 单空格)，
    经 splitlines/JSON 传输尾部空格被 strip → "" → 旧逻辑直接删除 → hunk 实际 context
    行数 < @@ 头声明的 old_count → git apply "补丁未应用/查询失败"。
    修复：_format_file_patch 把 "" 还原为 " "(单空格 context 标记)，而非删除。

    注：worker 已改用本地 git diff(同源)，不再产生 difflib 时代的 "\\n\\n" 行尾翻倍，
    故此处只需保证"被 strip 的空 context 行"被正确还原。
    """
    # @@ -1,4 表示 old 有 4 行：class A / 空行 / void f / }（第2行是空行 context）
    # 模拟传输 strip：空 context 行 " " 被 rstrip 成 ""
    stripped = (
        "--- a/A.java\n+++ b/A.java\n@@ -1,4 +1,5 @@\n class A {\n\n"  # 第2行本应是 " "，被 strip 成 ""
        "     void f() {}\n+    void g() {}\n }\n"
    )
    r = merge_diffs([("st-1", stripped)])
    assert r.success
    # 还原后 git apply 应成功（空 context 行还原为 " "，hunk 行数对齐）
    rc, err = _git_apply_check(r.merged_diff, {"A.java": "class A {\n\n    void f() {}\n}\n"})
    assert rc == 0, f"空 context 行被 strip 后未还原导致 apply 失败: {err}\ndiff={r.merged_diff!r}"


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
