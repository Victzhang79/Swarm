"""task dab669bb 回归：修复 B —— replan 守卫，保护已成功的兄弟子任务。

背景：medium 任务拆成 st-1(实现)+st-2(测试)，st-1 成功 DONE、st-2 因 L1 失败 →
LLM 选 strategy=replan → 原逻辑清空【含成功的 st-1】全部重新规划(~10min) → 死循环。
修复：handle_failure 在 strategy=replan 时，若存在已成功(L1 通过)的兄弟子任务且失败
子任务未达重试上限，降级为 retry（只重做失败的，保留成功成果），不全量 replan。
"""
import asyncio
from unittest.mock import patch

import pytest

from swarm.brain.nodes import handle_failure
from swarm.types import WorkerOutput


def _wo(sid, l1_passed, summary=""):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if l1_passed else "",
        summary=summary,
        l1_passed=l1_passed,
        l1_details={},
        confidence="high" if l1_passed else "low",
    )


def _run(state):
    return asyncio.run(handle_failure(state))


def test_replan_guard_preserves_successful_siblings(monkeypatch):
    """st-1 成功 + st-2 失败 + LLM 选 replan → 应降级 retry，只重做 st-2，保留 st-1。"""
    # mock LLM 返回 replan
    async def _fake_invoke(self, msgs):
        class R:
            content = '{"strategy": "replan", "reasoning": "st-2 写错测试"}'
        return R()

    state = {
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {
            "st-1": _wo("st-1", True, "实现完成"),
            "st-2": _wo("st-2", False, "JUnit 用错"),
        },
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
    }
    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        inst = mock_llm.return_value
        inst.ainvoke = _fake_invoke.__get__(inst)
        result = _run(state)

    # 应走 retry（不是 replan），保留 st-1，只重派 st-2
    assert result["failure_strategy"] in ("retry", "retry_alternate"), result["failure_strategy"]
    assert "st-1" in result["subtask_results"], "成功的 st-1 被清空了！"
    assert "st-2" not in result["subtask_results"], "失败的 st-2 应被移除待重做"
    assert "st-2" in result["dispatch_remaining"], "st-2 应放回待派发队列"
    # 不应进入全量 replan（plan_valid 不应为 False）
    assert result.get("plan_valid") is not False


def test_replan_proceeds_when_all_failed(monkeypatch):
    """整批都失败（无成功兄弟）→ 仍走原 replan（可能真是计划问题）。"""
    async def _fake_invoke(self, msgs):
        class R:
            content = '{"strategy": "replan", "reasoning": "计划有问题"}'
        return R()

    state = {
        "failed_subtask_ids": ["st-1", "st-2"],
        "subtask_results": {
            "st-1": _wo("st-1", False),
            "st-2": _wo("st-2", False),
        },
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "replan_count": 0,
    }
    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        inst = mock_llm.return_value
        inst.ainvoke = _fake_invoke.__get__(inst)
        result = _run(state)

    # 无成功兄弟 → 走原 replan
    assert result["failure_strategy"] == "replan", result["failure_strategy"]
