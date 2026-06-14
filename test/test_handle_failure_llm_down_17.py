"""audit #17 修复回归测试：handle_failure 在 LLM 不可用时不崩溃（strategy 默认值）。

原 bug（比 audit 描述更严重）：strategy 未在 try 前初始化，若 _get_brain_llm() 直接
抛异常，except 后 `if strategy == "replan"` 会 NameError。修复后默认 "retry"。

构造态：非 SIMPLE 复杂度走 LLM 分析路径，patch _get_brain_llm 抛异常，断言不崩 +
failure_strategy 为确定性 retry。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


def test_handle_failure_survives_llm_down():
    """_get_brain_llm 抛异常时 handle_failure 不应 NameError，回退确定性 retry。"""
    def _boom():
        raise RuntimeError("brain LLM down")

    plan = TaskPlan(
        subtasks=[
            SubTask(
                id="st-1",
                description="d",
                difficulty=SubTaskDifficulty.MEDIUM,
                modality=SubTaskModality.TEXT,
                scope=FileScope(writable=["a.py"]),
            )
        ],
        parallel_groups=[["st-1"]],
    )
    state = {
        "complexity": Complexity.MEDIUM,  # 非 SIMPLE → 走 LLM 分析路径
        "plan": plan,
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": WorkerOutput(subtask_id="st-1", diff="", summary="x", l1_passed=False)},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }

    with patch.object(nodes, "_get_brain_llm", side_effect=_boom):
        out = asyncio.run(nodes.handle_failure(state))

    # 不崩 + 回退到确定性 retry（不是 replan/escalate）
    assert isinstance(out, dict)
    assert out.get("failure_strategy") in ("retry", "retry_alternate"), out.get("failure_strategy")


if __name__ == "__main__":
    try:
        test_handle_failure_survives_llm_down()
        print("  ✅ test_handle_failure_survives_llm_down")
        print("\n=== audit #17 handle_failure NameError fix: 1/1 passed ===")
    except Exception as e:
        print(f"  💥 {type(e).__name__}: {e}")
        raise
