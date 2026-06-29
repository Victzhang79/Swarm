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


# ── R1a：失败子任务【耗尽重试】但有成功兄弟 → escalate 保留成果，绝不全量 replan clobber ──

def test_exhausted_capability_failure_escalates_preserving_siblings(monkeypatch):
    """996db614 主失控修复：st-1 成功 + st-2 能力失败且耗尽重试 → escalate(保留 st-1)，
    不再落全量 replan 清空 34 完成。"""
    async def _fake_invoke(self, msgs):
        class R:
            content = '{"strategy": "replan", "reasoning": "CipherUtils 方法幻觉，修不动"}'
        return R()

    state = {
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {
            "st-1": _wo("st-1", True, "实现完成"),
            "st-2": _wo("st-2", False, "CipherUtils 幻觉"),
        },
        "subtask_retry_counts": {"st-2": 3},  # 已耗尽（max_retries 默认 2，_deepest=4>3）
        "dispatch_remaining": [],
        "replan_count": 0,
    }
    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        inst = mock_llm.return_value
        inst.ainvoke = _fake_invoke.__get__(inst)
        result = _run(state)

    assert result["failure_strategy"] == "escalate", result["failure_strategy"]
    assert result.get("failure_escalated") is True
    # 关键：成功的 st-1 必须保留，绝不被 replan 清空
    assert "st-1" in result["subtask_results"], "成功成果被清空=主失控未修！"


# ── R1b：外科手术式 replan reset —— 按签名保留一致的完成态 ──

def test_surgical_replan_reset_preserves_unchanged_completed():
    from swarm.brain.nodes import _surgical_replan_reset
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    def _st(sid, desc, writable):
        return SubTask(id=sid, description=desc, difficulty=SubTaskDifficulty.MEDIUM,
                       scope=FileScope(writable=writable))

    old_plan = TaskPlan(subtasks=[_st("st-1", "建A", ["a.java"]), _st("st-2", "建B", ["b.java"])])
    new_plan = TaskPlan(subtasks=[
        _st("st-1", "建A", ["a.java"]),      # 签名完全一致 → 保留
        _st("st-2", "建B改了", ["b.java"]),  # 描述变 → 不保留（语义变）
        _st("st-3", "建C", ["c.java"]),      # 新增 → 不在
    ])
    old_results = {"st-1": _wo("st-1", True), "st-2": _wo("st-2", True)}
    reset = _surgical_replan_reset(old_results, old_plan, new_plan)
    assert "st-1" in reset["subtask_results"], "签名一致的应保留(dispatch 自动跳过)"
    assert "st-2" not in reset["subtask_results"], "描述变=语义变→应重派(防 premature victory)"
    assert "st-3" not in reset["subtask_results"]
    assert reset["dispatch_remaining"] == [] and reset["failed_subtask_ids"] == []


def test_surgical_replan_reset_empty_and_unpassed():
    from swarm.brain.nodes import _surgical_replan_reset
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    # 首规划无旧态 → 空 reset
    assert _surgical_replan_reset({}, None, None) == {}
    # 旧结果 L1 未过 → 不保留（只保留通过的，防把坏结果当完成）
    def _st(sid):
        return SubTask(id=sid, description="X", difficulty=SubTaskDifficulty.MEDIUM,
                       scope=FileScope(writable=["a.java"]))
    p = TaskPlan(subtasks=[_st("st-1")])
    reset = _surgical_replan_reset({"st-1": _wo("st-1", False)}, p, p)
    assert "st-1" not in reset.get("subtask_results", {})
