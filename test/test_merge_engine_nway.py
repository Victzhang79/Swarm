"""A-P1-26(b): N-way 三方折叠的特征化(characterization)测试 —— 锁定正确合并行为。

背景：merge_engine._try_three_way_resolve 对 >2 个冲突子任务做链式三方折叠：
所有 versions[sid] = apply_hunks_to_text(base_raw, hunks) 都从同一个共同祖先
base_raw(=HEAD)分叉；折叠 three_way_merge_text(base_raw, merged_text, versions[C])
等价于 git merge-file ours=merged_text base=base_raw theirs=C，正确累加 base+A+B+C。

这些测试是 CHARACTERIZATION（行为不变），用来防止未来有人把"固定 base"误"修"成
"演进 base"——那样会丢掉更早分支的改动（回归）。它们断言:
  1. 三个子任务各自做不同改动且 hunk 范围重叠(触发三方折叠路径)时,
     三处改动全部出现在合并结果里(证明固定 base 折叠正确累加)。
  2. 其中两个子任务在同一锚点冲突时,不会静默把两个版本都塞进去(不伪造数据),
     而是升级到 rebase/冲突路径。
"""

from __future__ import annotations

from swarm.brain.merge_engine import merge_diffs

# 12 行基底文件，作为三个子任务共同的祖先(HEAD)。
BASE_F_PY = "".join(f"line{i}\n" for i in range(1, 13))


def _reader(path: str) -> str | None:
    return BASE_F_PY if path == "f.py" else None


# 三个子任务各替换一行(line3 / line6 / line9)，但故意给每个 hunk 较宽的上下文范围，
# 使它们的 old-range 互相重叠 → 触发冲突检测 → 走 _try_three_way_resolve 的三方折叠。
# 改动本身彼此不冲突(改不同行)，固定 base 折叠应当把三处都累加进来。
DIFF_A = (
    "--- a/f.py\n+++ b/f.py\n"
    "@@ -2,7 +2,7 @@\n line2\n-line3\n+AAA_a\n line4\n line5\n line6\n line7\n line8\n"
)
DIFF_B = (
    "--- a/f.py\n+++ b/f.py\n"
    "@@ -3,7 +3,7 @@\n line3\n line4\n line5\n-line6\n+BBB_b\n line7\n line8\n line9\n"
)
DIFF_C = (
    "--- a/f.py\n+++ b/f.py\n"
    "@@ -6,7 +6,7 @@\n line6\n line7\n line8\n-line9\n+CCC_c\n line10\n line11\n line12\n"
)


def test_nway_fold_accumulates_all_three_changes():
    """3 个重叠子任务经固定-base 三方折叠后,三处改动全部保留(无声丢失=回归)。"""
    result = merge_diffs(
        [("st-a", DIFF_A), ("st-b", DIFF_B), ("st-c", DIFF_C)],
        base_reader=_reader,
        auto_resolve=True,
    )
    assert result.success is True, result.merged_diff
    assert result.conflicts == []
    assert result.rebase_subtask_ids == []
    # 关键断言:三处独立改动都在,证明折叠以单一共同祖先 base_raw 累加 A+B+C。
    # 若有人把 base 改成"演进版"(用上一轮 merged_text 当 base),会丢掉更早分支的改动。
    assert "AAA_a" in result.merged_diff, result.merged_diff
    assert "BBB_b" in result.merged_diff, result.merged_diff
    assert "CCC_c" in result.merged_diff, result.merged_diff


# 两个子任务在同一锚点(line3)替换为【不同】内容 → 真冲突；第三个子任务改动独立。
DIFF_CONFLICT_A = (
    "--- a/f.py\n+++ b/f.py\n@@ -2,3 +2,3 @@\n line2\n-line3\n+CONFLICT_a\n line4\n"
)
DIFF_CONFLICT_B = (
    "--- a/f.py\n+++ b/f.py\n@@ -2,3 +2,3 @@\n line2\n-line3\n+CONFLICT_b\n line4\n"
)
DIFF_INDEP_C = (
    "--- a/f.py\n+++ b/f.py\n@@ -8,1 +8,2 @@\n line8\n+CCC_c\n"
)


def test_nway_conflict_does_not_silently_merge():
    """同锚点冲突不被静默"两个版本都塞进去"(不伪造数据),而是升级到 rebase。"""
    result = merge_diffs(
        [("st-a", DIFF_CONFLICT_A), ("st-b", DIFF_CONFLICT_B), ("st-c", DIFF_INDEP_C)],
        base_reader=_reader,
        auto_resolve=True,
    )
    # 绝不把两个冲突版本同时塞进结果(那是无声吞冲突/伪造合并)。
    assert not (
        "CONFLICT_a" in result.merged_diff and "CONFLICT_b" in result.merged_diff
    ), "同锚点冲突被静默拼接(伪造数据):" + result.merged_diff
    # 升级路径:其中一个冲突子任务被标记待 rebase 重生成,而非凭空合并。
    assert result.rebase_subtask_ids, "冲突应升级到 rebase,而非静默成功:" + result.merged_diff
