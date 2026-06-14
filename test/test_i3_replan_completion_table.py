"""I3 单测：plan 节点 replan 重入时清空完成态事实表，防 premature victory。

风险：replan 后新 plan 可能复用旧子任务 id，旧"成功"结果会让新子任务被误判已完成而
跳过执行（premature victory）。修复：plan 检测到 state 已有 subtask_results（重入信号）→
确定性清空 subtask_results / dispatch_remaining / failed_subtask_ids。

用 SIMPLE 路径（不调 LLM）测重置逻辑，纯 state 探针，无存储依赖。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.nodes import plan
from swarm.types import Complexity


def _simple_state(**extra):
    # SIMPLE 复杂度走 _build_simple_plan，不调 LLM
    s = {
        "task_description": "fix typo in README",
        "complexity": Complexity.SIMPLE,
        "affected_files": ["README.md"],
    }
    s.update(extra)
    return s


async def test_first_plan_no_reset():
    """首次规划（无旧 subtask_results）→ 不含重置字段。"""
    out = await plan(_simple_state())
    assert "plan" in out
    assert out.get("subtask_results") in (None, {}) or "subtask_results" not in out
    print("  ✅ 首次规划不触发重置")


async def test_replan_reentry_clears_completion_table():
    """replan 重入（state 已有 subtask_results）→ 清空完成态/派发队列/失败列表。"""
    out = await plan(_simple_state(
        subtask_results={"st-old-1": object(), "st-old-2": object()},
        dispatch_remaining=["st-old-3"],
        failed_subtask_ids=["st-old-2"],
    ))
    assert "plan" in out
    assert out.get("subtask_results") == {}, "replan 必须清空旧完成态"
    assert out.get("dispatch_remaining") == [], "replan 必须清空派发队列"
    assert out.get("failed_subtask_ids") == [], "replan 必须清空失败列表"
    print("  ✅ replan 重入清空完成态事实表（防 premature victory）")


async def test_replan_reset_overrides_stale_completion():
    """核心防线：旧完成态被清空后，新 plan 的子任务不会因旧 id 撞车被误判已完成。"""
    out = await plan(_simple_state(subtask_results={"st-1": object()}))
    assert out.get("subtask_results") == {}
    print("  ✅ 旧 id 撞车不再导致 premature victory")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
