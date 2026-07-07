"""P0-3 治本单测：MERGE 引擎 diff 解析静默丢产物簇（D03/D04/D05/D06）。

四条缺陷的共性=产出的 merged_diff 是 well-formed 的，骗过 apply-check/L2 编译，属【静默丢内容】：
- D03：`+++ /dev/null` 被 `plus[6:]` 切成 "ev/null" → 删除补丁归到伪路径 → 删除整体蒸发。
- D04：3-way 链式合并只折叠冲突参与者 → 同文件第三写者(非冲突)的 hunk 被静默丢弃。
- D05：hunk 体内 `--- ` 开头的删除行(如删 SQL `-- comment`)被当文件边界 → 截断 hunk。
- D06：纯 rename 段 / rename+edit / `GIT binary patch` / `Binary files ... differ` 被静默丢弃。

行为断言(禁 getsource)：构造最小 unified diff，断言 merged_diff 保留应有内容。
"""

from __future__ import annotations

from swarm.brain.merge_engine import merge_diffs


# ─────────────────────────── D03：删除文件补丁 ───────────────────────────

_DELETE_DIFF = (
    "diff --git a/foo/legacy.txt b/foo/legacy.txt\n"
    "deleted file mode 100644\n"
    "--- a/foo/legacy.txt\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-line1\n"
    "-line2\n"
    "-line3\n"
)

_LEGACY_BASE = "line1\nline2\nline3\n"


def test_d03_deletion_patch_not_lost_to_pseudo_path():
    """删除子任务经 merge → merged_diff 含正确删除段(不是伪路径 ev/null，不蒸发)。"""

    def reader(path: str) -> str | None:
        return _LEGACY_BASE if path == "foo/legacy.txt" else None

    res = merge_diffs([("st-del", _DELETE_DIFF)], base_reader=reader)
    md = res.merged_diff
    # 伪路径蒸发的证据：旧代码 file_path=="ev/null"（`+++ /dev/null`[6:]），删除内容全丢。
    assert "a/ev/null" not in md and "b/ev/null" not in md, f"删除被归并到伪路径 ev/null: {md!r}"
    # 删除必须表达为 /dev/null 目标 + 全部 `-` 行 + 正确真实路径。
    assert "foo/legacy.txt" in md
    assert "+++ /dev/null" in md
    assert "-line1" in md and "-line2" in md and "-line3" in md
    assert "deleted file mode" in md


# ─────────────────────────── D04：三方折叠丢第三写者 ───────────────────────────

# 25 行基底：A 改 L3、B 改 L6（hunk old-range 重叠 → 进冲突集但内容不冲突 → 3-way 干净消解），
# C 改 L20（远处非冲突 hunk）。旧代码 3-way 只链冲突参与者 {A,B} → C 被静默丢。
_BASE25 = "".join(f"L{i}\n" for i in range(1, 26))

_A_DIFF = (
    "--- a/f.txt\n"
    "+++ b/f.txt\n"
    "@@ -1,5 +1,5 @@\n"
    " L1\n L2\n-L3\n+A3\n L4\n L5\n"
)
_B_DIFF = (
    "--- a/f.txt\n"
    "+++ b/f.txt\n"
    "@@ -4,5 +4,5 @@\n"
    " L4\n L5\n-L6\n+B6\n L7\n L8\n"
)
_C_DIFF = (
    "--- a/f.txt\n"
    "+++ b/f.txt\n"
    "@@ -18,5 +18,5 @@\n"
    " L18\n L19\n-L20\n+C20\n L21\n L22\n"
)


def test_d04_three_way_folds_nonconflict_third_writer():
    """A/B 冲突(重叠但可消解)+C 非冲突同文件 → merged_diff 必含 C 的改动。"""

    def reader(path: str) -> str | None:
        return _BASE25 if path == "f.txt" else None

    res = merge_diffs(
        [("st-a", _A_DIFF), ("st-b", _B_DIFF), ("st-c", _C_DIFF)],
        base_reader=reader,
    )
    md = res.merged_diff
    assert "A3" in md, f"A 的改动丢失: {md!r}"
    assert "B6" in md, f"B 的改动丢失: {md!r}"
    # 核心：第三写者(非冲突) C 的改动绝不能被 3-way 折叠静默丢弃。
    assert "C20" in md, f"第三写者 C 的非冲突改动被静默丢弃: {md!r}"


# ─────────────────────────── D05：hunk 体内 `--- ` 删除行 ───────────────────────────

# 删除一行 SQL 注释 `-- old comment` → diff 里该删除行渲成 `--- old comment`。
_SQL_BASE = "CREATE TABLE t;\n-- old comment\nINSERT INTO t;\nSELECT 1;\n"
_SQL_DIFF = (
    "--- a/schema.sql\n"
    "+++ b/schema.sql\n"
    "@@ -1,4 +1,3 @@\n"
    " CREATE TABLE t;\n"
    "--- old comment\n"
    " INSERT INTO t;\n"
    " SELECT 1;\n"
)


def test_d05_deletion_line_in_hunk_not_treated_as_file_boundary():
    """hunk 体内以 `--- ` 开头的删除行不得被当文件边界截断 hunk。"""

    def reader(path: str) -> str | None:
        return _SQL_BASE if path == "schema.sql" else None

    res = merge_diffs([("st-sql", _SQL_DIFF)], base_reader=reader)
    md = res.merged_diff
    # 截断证据：旧代码在 `--- old comment` 处断开 → INSERT/SELECT 行整体丢弃。
    assert "INSERT INTO t;" in md, f"hunk 被 `--- ` 行截断，尾部丢失: {md!r}"
    assert "SELECT 1;" in md, f"hunk 被 `--- ` 行截断，尾部丢失: {md!r}"
    # 被删除的注释行本身也应保留在 hunk 体内(作为 `-` 删除)。
    assert "--- old comment" in md


# ─────────────────────────── D06：rename / binary 段 ───────────────────────────

_PURE_RENAME_DIFF = (
    "diff --git a/old/path.txt b/new/path.txt\n"
    "similarity index 100%\n"
    "rename from old/path.txt\n"
    "rename to new/path.txt\n"
)


def test_d06_pure_rename_segment_preserved():
    """纯 rename 段(无 hunk)不得被静默丢弃。"""
    res = merge_diffs([("st-rn", _PURE_RENAME_DIFF)])
    md = res.merged_diff
    assert "rename from old/path.txt" in md, f"纯 rename 段被丢弃: {md!r}"
    assert "rename to new/path.txt" in md


_RENAME_EDIT_DIFF = (
    "diff --git a/old.txt b/new.txt\n"
    "similarity index 90%\n"
    "rename from old.txt\n"
    "rename to new.txt\n"
    "--- a/old.txt\n"
    "+++ b/new.txt\n"
    "@@ -1,2 +1,2 @@\n"
    " keep\n"
    "-old\n"
    "+new\n"
)


def test_d06_rename_with_edit_preserved():
    """rename+edit 段：rename 元数据与 hunk 都不得丢，旧路径不得漏删。"""
    res = merge_diffs([("st-re", _RENAME_EDIT_DIFF)])
    md = res.merged_diff
    assert "rename from old.txt" in md and "rename to new.txt" in md, f"rename 元数据丢失: {md!r}"
    assert "+new" in md, f"rename 伴随的编辑 hunk 丢失: {md!r}"


_BINARY_LITERAL_DIFF = (
    "diff --git a/img.bin b/img.bin\n"
    "index 0000000..1111111 100644\n"
    "GIT binary patch\n"
    "literal 8\n"
    "zcmZQ7000000000\n"
    "\n"
    "literal 0\n"
    "HcmV?d00001\n"
)


def test_d06_git_binary_patch_preserved():
    """`GIT binary patch` 段无法字符级合并 → 必须整段透传，绝不静默丢弃。"""
    res = merge_diffs([("st-bin", _BINARY_LITERAL_DIFF)])
    md = res.merged_diff
    assert "GIT binary patch" in md, f"二进制补丁段被丢弃: {md!r}"
    assert "zcmZQ7000000000" in md, f"二进制载荷丢失: {md!r}"


_BINARY_DIFFER_DIFF = (
    "diff --git a/logo.png b/logo.png\n"
    "index abc1234..def5678 100644\n"
    "Binary files a/logo.png and b/logo.png differ\n"
)


def test_d06_binary_files_differ_preserved():
    """`Binary files ... differ` 段同样必须透传保留。"""
    res = merge_diffs([("st-bd", _BINARY_DIFFER_DIFF)])
    md = res.merged_diff
    assert "Binary files a/logo.png and b/logo.png differ" in md, f"二进制 differ 段被丢弃: {md!r}"


# ──────────────── hunter#1 复核整改：删除专路多写者（D03 补强） ────────────────

_MODIFY_LEGACY_DIFF = (
    "diff --git a/foo/legacy.txt b/foo/legacy.txt\n"
    "--- a/foo/legacy.txt\n"
    "+++ b/foo/legacy.txt\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n"
    "-line2\n"
    "+line2-improved\n"
    " line3\n"
)


def _legacy_reader(path: str) -> str | None:
    return _LEGACY_BASE if path == "foo/legacy.txt" else None


def test_hunter1_double_delete_same_file_dedupes_to_single_valid_patch():
    """两个子任务各自独立删除同一文件（良性一致意图）→ 删除 hunk 去重成单份，
    不得拼接出重复 `@@ -1,3 +0,0 @@` 的非法补丁（git apply 必败）。"""
    res = merge_diffs(
        [("st-a", _DELETE_DIFF), ("st-b", _DELETE_DIFF)], base_reader=_legacy_reader,
    )
    assert res.success is True, f"一致的双删除不应报冲突: {res.conflicts}"
    md = res.merged_diff
    assert "+++ /dev/null" in md, f"删除段蒸发: {md!r}"
    assert md.count("-line1") == 1, f"删除 hunk 未去重（重复 apply 必败）: {md!r}"
    assert md.count("@@ -1,3 +0,0 @@") == 1, f"重复删除 hunk 头: {md!r}"


def test_hunter1_delete_vs_modify_reports_honest_conflict():
    """一个子任务删除、另一个修改同一文件 → 真实协同冲突：必须以 MergeConflict 如实上报
    （交冲突机制处理），绝不把修改 hunk 拼进 `+++ /dev/null` 段产出非法补丁
    （`removed file still has content`），也不得误标成 merge 引擎组装缺陷。"""
    res = merge_diffs(
        [("st-del", _DELETE_DIFF), ("st-mod", _MODIFY_LEGACY_DIFF)],
        base_reader=_legacy_reader,
    )
    assert res.success is False, "delete-vs-modify 是真冲突，不应静默当成功"
    assert any(
        c.file_path == "foo/legacy.txt"
        and {"st-del", "st-mod"} <= set(c.subtask_ids)
        for c in res.conflicts
    ), f"冲突未如实上报: {res.conflicts}"
    md = res.merged_diff
    if "+++ /dev/null" in md:
        seg = md[md.index("+++ /dev/null"):]
        assert "+line2-improved" not in seg, f"修改 hunk 被拼进删除段（非法补丁）: {md!r}"
