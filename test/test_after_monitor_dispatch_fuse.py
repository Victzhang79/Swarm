#!/usr/bin/env python3
"""#R13-4 治本单测 —— MONITOR 后路由的【派发面终态熔断】。

round13 实测缺陷：阶梯三 give-up-preserve 后，剩余子任务全部依赖已 revert/放弃的上游 →
永不就绪，但 dispatch_remaining 非空 → 旧逻辑"剩余非空即 DISPATCH" → get_dispatch_batch
空批不排空 → MONITOR→DISPATCH 紧致空转撞 recursion_limit → 整任务 FAILED（非 PARTIAL）。
治本：after_monitor 用与 DISPATCH 相同就绪判定探测；无一可派发 → 转 MERGE(PARTIAL)。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.graph import after_monitor
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _sub(sid, deps=None):
    return SubTask(
        id=sid, description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[], readable=[]), depends_on=deps or [],
    )


def test_stuck_remaining_all_blocked_routes_merge():
    """核心：剩余子任务全依赖【已放弃】上游 → 永不就绪 → 转 MERGE，不空转 DISPATCH。"""
    plan = TaskPlan(subtasks=[_sub("st-up"), _sub("st-down", deps=["st-up"])])
    state = {
        "plan": plan,
        "dispatch_remaining": ["st-down"],   # 非空
        "failed_subtask_ids": [],
        "subtask_results": {},               # st-up 未完成(被 revert/放弃)
        "abandoned_subtask_ids": ["st-up"],  # st-up 在放弃集 → st-down 永不就绪
    }
    assert after_monitor(state) == "merge", "剩余全不可派发应转 MERGE(PARTIAL)，不再空转"
    print("  ✅ 剩余全依赖放弃项 → MONITOR → MERGE（#R13-4 熔断，杜绝 recursion_limit 空转）")


def test_stuck_via_give_up_isolated_routes_merge():
    """give_up_isolated_ids（阶梯三另一来源）同样触发熔断。"""
    plan = TaskPlan(subtasks=[_sub("st-a"), _sub("st-b", deps=["st-a"])])
    state = {
        "plan": plan, "dispatch_remaining": ["st-b"], "failed_subtask_ids": [],
        "subtask_results": {}, "give_up_isolated_ids": ["st-a"],
    }
    assert after_monitor(state) == "merge"
    print("  ✅ give_up_isolated 上游 → 下游熔断转 MERGE")


def test_dispatchable_remaining_still_routes_dispatch():
    """回归防护：仍有可派发子任务时【绝不】误终结，照常 DISPATCH。"""
    plan = TaskPlan(subtasks=[_sub("st-1"), _sub("st-2", deps=["st-1"])])
    state = {
        "plan": plan, "dispatch_remaining": ["st-1", "st-2"],
        "failed_subtask_ids": [], "subtask_results": {},
    }
    assert after_monitor(state) == "dispatch", "st-1 无依赖可派发 → 必须 DISPATCH"
    print("  ✅ 有可派发子任务 → 照常 DISPATCH（不误熔断）")


def test_failed_takes_priority():
    """失败优先级最高 → HANDLE_FAILURE（熔断不抢占失败处理）。"""
    state = {"failed_subtask_ids": ["x"], "dispatch_remaining": ["y"], "plan": None}
    assert after_monitor(state) == "handle_failure"
    print("  ✅ 有失败 → HANDLE_FAILURE 优先")


def test_all_done_routes_merge():
    state = {"dispatch_remaining": [], "failed_subtask_ids": []}
    assert after_monitor(state) == "merge"
    print("  ✅ 全部完成 → MERGE")


def test_no_plan_falls_back_to_dispatch():
    """plan 缺失(异常) → 保守回退 DISPATCH，绝不据空信息误终结。"""
    state = {"plan": None, "dispatch_remaining": ["z"], "failed_subtask_ids": []}
    assert after_monitor(state) == "dispatch"
    print("  ✅ plan 缺失 → 保守回退 DISPATCH（fail-safe，不误终结）")


if __name__ == "__main__":
    print("=" * 60)
    print("  #R13-4 派发面终态熔断单测")
    print("=" * 60)
    passed = failed = 0
    for t in [
        test_stuck_remaining_all_blocked_routes_merge,
        test_stuck_via_give_up_isolated_routes_merge,
        test_dispatchable_remaining_still_routes_dispatch,
        test_failed_takes_priority, test_all_done_routes_merge,
        test_no_plan_falls_back_to_dispatch,
    ]:
        try:
            t(); passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}"); failed += 1
    print("=" * 60)
    print(f"  📊 {passed} 通过, {failed} 失败")
    import sys
    sys.exit(1 if failed else 0)
