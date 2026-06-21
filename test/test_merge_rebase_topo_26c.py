#!/usr/bin/env python3
"""A-P1-26(c)：rebase base 选取按【依赖拓扑序】而非 hunk 出现序 — 特征化测试。

场景：两个子任务在同一锚点(line3)替换为不同内容 → 3-way 解不了 → 走 rebase 策略。
rebase 须以【依赖上游】为 base 保留其 diff、把【下游】标记 rebase 重生成。
本测试锁定：传入的 subtask_order(拓扑序)能驱动 base 选取，覆盖 hunk 出现序。
"""

from __future__ import annotations

from swarm.brain.merge_engine import merge_diffs

BASE_F_PY = "".join(f"line{i}\n" for i in range(1, 13))


def _reader(path: str) -> str | None:
    return BASE_F_PY if path == "f.py" else None


# 同锚点(line3)不同替换 → 真冲突 → rebase 路径
DIFF_A = "--- a/f.py\n+++ b/f.py\n@@ -2,3 +2,3 @@\n line2\n-line3\n+CONFLICT_a\n line4\n"
DIFF_B = "--- a/f.py\n+++ b/f.py\n@@ -2,3 +2,3 @@\n line2\n-line3\n+CONFLICT_b\n line4\n"


def test_topo_order_drives_base_over_appearance():
    """出现序 st-a 在前，但拓扑序声明 st-b 为上游 → base 应为 st-b（保留 CONFLICT_b）。"""
    result = merge_diffs(
        [("st-a", DIFF_A), ("st-b", DIFF_B)],  # 出现序：st-a 先
        base_reader=_reader,
        auto_resolve=True,
        subtask_order=["st-b", "st-a"],  # 拓扑序：st-b 上游
    )
    assert "CONFLICT_b" in result.merged_diff, result.merged_diff
    assert "CONFLICT_a" not in result.merged_diff, result.merged_diff
    assert result.rebase_subtask_ids == ["st-a"], result.rebase_subtask_ids
    print("  ✅ 拓扑序覆盖出现序：上游 st-b 当 base，st-a 待 rebase")


def test_default_falls_back_to_appearance_order():
    """无 subtask_order → 退回旧行为：出现序首个(st-a)为 base。"""
    result = merge_diffs(
        [("st-a", DIFF_A), ("st-b", DIFF_B)],
        base_reader=_reader,
        auto_resolve=True,
    )
    assert "CONFLICT_a" in result.merged_diff, result.merged_diff
    assert result.rebase_subtask_ids == ["st-b"], result.rebase_subtask_ids
    print("  ✅ 缺拓扑序退回出现序（向后兼容）：st-a 当 base，st-b 待 rebase")


def test_topological_order_helper():
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    def _st(sid, deps):
        return SubTask(
            id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
            scope=FileScope(writable=[f"{sid}.py"]), depends_on=deps,
        )

    # c 依赖 b，b 依赖 a → 拓扑序 a,b,c（即便 subtasks 列表乱序给出）
    plan = TaskPlan(subtasks=[_st("c", ["b"]), _st("a", []), _st("b", ["a"])])
    order = plan.topological_order()
    assert order.index("a") < order.index("b") < order.index("c"), order
    print("  ✅ TaskPlan.topological_order 正确拓扑排序（被依赖者在前）")


def test_topological_order_cycle_safe():
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    def _st(sid, deps):
        return SubTask(
            id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
            scope=FileScope(writable=[f"{sid}.py"]), depends_on=deps,
        )

    # 环 a->b->a：不丢子任务，按原序兜底
    plan = TaskPlan(subtasks=[_st("a", ["b"]), _st("b", ["a"])])
    order = plan.topological_order()
    assert set(order) == {"a", "b"}, order
    print("  ✅ TaskPlan.topological_order 环存在时不丢子任务（稳定兜底）")


def main() -> int:
    failed = 0
    for fn in (
        test_topo_order_drives_base_over_appearance,
        test_default_falls_back_to_appearance_order,
        test_topological_order_helper,
        test_topological_order_cycle_safe,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    print(f"\n{'FAIL' if failed else 'All passed'}: {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
