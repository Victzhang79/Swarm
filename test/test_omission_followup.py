#!/usr/bin/env python3
"""遗漏复查后续修复的回归测试（B18 plan 损坏 / A5 非 ULTRA 旁路）。"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_b18_revision_plan_stays_taskplan():
    """P0 回归：revision 经 resolve_plan_conflicts 后 plan 仍是 TaskPlan，不被替换成 dict/bool。"""
    import swarm.brain.nodes as nodes
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=["a.py"], readable=["a.py"]))],
        parallel_groups=[["st-1"]],
    )
    state = {
        "plan": plan, "revision_feedback": "请修复排序", "merged_diff": "",
        "task_description": "t", "subtask_results": {},
    }
    # LLM 失败 → 走默认修订子任务分支，再经 resolve_plan_conflicts（原地变更）。
    with patch.object(nodes, "_get_brain_llm", side_effect=RuntimeError("no llm")):
        out = asyncio.run(nodes.revision(state))
    assert isinstance(out["plan"], TaskPlan), f"plan 应仍是 TaskPlan，实为 {type(out['plan'])}"
    assert out["plan"].subtasks, "plan.subtasks 应可访问（未损坏）"
    print("  ✅ B18：修订后 plan 仍是 TaskPlan（防 dict/bool 损坏）")


def test_a5_after_validate_blocks_degraded_plan():
    """A5 补漏：非 ULTRA + plan_generation_failed → 路由 confirm（不直接 dispatch 假计划）。"""
    from swarm.brain.graph import after_validate

    degraded = {"plan_valid": True, "plan_generation_failed": True, "complexity": "medium"}
    assert after_validate(degraded) == "confirm", "降级假计划必须走 confirm 拦截"
    # 对照：正常计划 → dispatch
    assert after_validate({"plan_valid": True, "complexity": "medium"}) == "dispatch"
    print("  ✅ A5：非 ULTRA 降级假计划走 confirm（不旁路 dispatch）")


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {fn.__name__}: {e}")
            fails += 1
    sys.exit(1 if fails else 0)
