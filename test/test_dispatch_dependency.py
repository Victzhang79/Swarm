#!/usr/bin/env python3
"""dispatch 依赖驱动并行单测（批次③）— 固化 get_dispatch_batch 行为，防回归。

验证 review skill #23 的修复：调度按 depends_on DAG 驱动并行，而非 LLM 的 parallel_groups。
- 独立子任务（无依赖）→ 同批并发
- 有依赖的子任务 → 依赖未完成时不派发（串行）
- max_concurrent 截断
- parallel_groups 不再阻断并行（即使 LLM 把独立任务拆进各自 group）
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _sub(sid, deps=None):
    return SubTask(
        id=sid, description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[], readable=[]), depends_on=deps or [],
    )


def test_independent_tasks_dispatch_together():
    # 两个独立任务，即使 LLM 把它们拆进各自 group，也应同批派发
    plan = TaskPlan(
        subtasks=[_sub("st-1"), _sub("st-2")],
        parallel_groups=[["st-1"], ["st-2"]],  # LLM 过度保守分组
    )
    batch = plan.get_dispatch_batch(set(), ["st-1", "st-2"], max_concurrent=4)
    ids = {t.id for t in batch}
    assert ids == {"st-1", "st-2"}, f"独立任务应同批，实际 {ids}"
    print("  ✅ 独立任务同批并发（不受 parallel_groups 拆分限制）")


def test_dependent_task_held_until_dep_done():
    # st-2 依赖 st-1：st-1 未完成时 st-2 不派发
    plan = TaskPlan(subtasks=[_sub("st-1"), _sub("st-2", deps=["st-1"])])
    batch1 = plan.get_dispatch_batch(set(), ["st-1", "st-2"], max_concurrent=4)
    assert {t.id for t in batch1} == {"st-1"}, "st-2 依赖未满足不应派发"
    print("  ✅ 有依赖任务在依赖完成前被串行held")
    # st-1 完成后，st-2 就绪
    batch2 = plan.get_dispatch_batch({"st-1"}, ["st-2"], max_concurrent=4)
    assert {t.id for t in batch2} == {"st-2"}, "st-1 完成后 st-2 应就绪"
    print("  ✅ 依赖完成后下游任务就绪派发")


def test_max_concurrent_caps_batch():
    plan = TaskPlan(subtasks=[_sub(f"st-{i}") for i in range(1, 6)])
    batch = plan.get_dispatch_batch(set(), [f"st-{i}" for i in range(1, 6)], max_concurrent=2)
    assert len(batch) == 2, f"应被 max_concurrent=2 截断，实际 {len(batch)}"
    print("  ✅ max_concurrent 截断批次大小")


def test_diamond_dag():
    # 钻石依赖: st-1 → (st-2, st-3) → st-4
    plan = TaskPlan(subtasks=[
        _sub("st-1"), _sub("st-2", deps=["st-1"]),
        _sub("st-3", deps=["st-1"]), _sub("st-4", deps=["st-2", "st-3"]),
    ])
    # 初始只有 st-1
    b1 = plan.get_dispatch_batch(set(), ["st-1", "st-2", "st-3", "st-4"], max_concurrent=4)
    assert {t.id for t in b1} == {"st-1"}
    # st-1 完成 → st-2, st-3 并发
    b2 = plan.get_dispatch_batch({"st-1"}, ["st-2", "st-3", "st-4"], max_concurrent=4)
    assert {t.id for t in b2} == {"st-2", "st-3"}, f"st-2/st-3 应并发，实际 {[t.id for t in b2]}"
    # st-2, st-3 完成 → st-4
    b3 = plan.get_dispatch_batch({"st-1", "st-2", "st-3"}, ["st-4"], max_concurrent=4)
    assert {t.id for t in b3} == {"st-4"}
    print("  ✅ 钻石 DAG：fan-out 并发 + fan-in 等齐后串行")


if __name__ == "__main__":
    print("=" * 56)
    print("  dispatch 依赖驱动并行单测（批次③）")
    print("=" * 56)
    passed = failed = 0
    for t in [test_independent_tasks_dispatch_together, test_dependent_task_held_until_dep_done,
              test_max_concurrent_caps_batch, test_diamond_dag]:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    print("=" * 56)
    print(f"  📊 结果: {passed} 通过, {failed} 失败")
    print("=" * 56)
    import sys
    sys.exit(1 if failed else 0)
