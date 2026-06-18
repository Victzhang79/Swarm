"""部分交付(PARTIAL) + 拒答重试走最强模型(FINDING-12)。

RUN5(task c99bcfef)：6 子任务/2 模块真编译代码，挂在 st-7 模型拒答(refusal_hard_fail，
'Sorry need more steps'=ReAct agent 撞 recursion_limit)。原 fail-fast：1 个子任务耗尽重试 →
failure_escalated → 整任务 FAILED，灭掉 6 个好子任务。

两道改进：
- 部分交付：重试耗尽 + 已有完成子任务 → 放弃失败者(+传递依赖者)继续交付其余，终态 PARTIAL(非 DONE)。
- FINDING-12：拒答/步数耗尽子任务重试强制走【最强模型】(40B 256k)，而非更弱 fallback。
红线：PARTIAL ≠ DONE；0 完成时仍 escalate(不假成功)。
"""
import asyncio
from unittest.mock import patch

from swarm.brain.nodes import handle_failure
from swarm.types import FileScope, SubTask, TaskPlan, TaskStatus, WorkerOutput


def _wo(sid, l1_passed, decision_source=None):
    return WorkerOutput(
        subtask_id=sid, diff="--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b\n" if l1_passed else "",
        summary="", l1_passed=l1_passed,
        l1_details={"l1_decision_source": decision_source} if decision_source else {},
        confidence="high" if l1_passed else "low",
    )


def _st(sid, depends=None):
    return SubTask(id=sid, description="d", scope=FileScope(writable=[f"{sid}.java"]),
                   depends_on=depends or [])


def _run(state):
    return asyncio.run(handle_failure(state))


def _patch_llm(m, strategy="retry"):
    async def _inv(_self, _msgs):
        class R:
            content = '{"strategy":"%s","reasoning":"x"}' % strategy
        return R()
    m.return_value.ainvoke = _inv.__get__(m.return_value)


def test_partial_status_enum_exists():
    assert TaskStatus.PARTIAL.value == "PARTIAL"
    assert TaskStatus.PARTIAL != TaskStatus.DONE


def test_partial_delivery_abandons_failed_and_continues():
    """重试耗尽 + 有完成子任务 → 放弃失败者(+依赖者)，继续交付其余，strategy=abandon（非 escalate）。"""
    plan = TaskPlan(subtasks=[_st("st-1"), _st("st-2"), _st("st-3"), _st("st-4", depends=["st-3"])])
    state = {
        "failed_subtask_ids": ["st-3"],
        "subtask_results": {"st-1": _wo("st-1", True), "st-2": _wo("st-2", True),
                            "st-3": _wo("st-3", False)},
        "subtask_retry_counts": {"st-3": 3},  # 已过 retry(2)+alternate(1) 上限
        "dispatch_remaining": ["st-4"],
        "plan": plan,
    }
    with patch("swarm.brain.nodes._get_brain_llm") as m:
        _patch_llm(m)
        r = _run(state)
    assert r["failure_strategy"] == "abandon", r["failure_strategy"]
    assert not r.get("failure_escalated"), "部分交付不应设 failure_escalated"
    assert set(r["abandoned_subtask_ids"]) == {"st-3", "st-4"}, \
        f"应放弃 st-3 + 传递依赖者 st-4: {r['abandoned_subtask_ids']}"
    assert r["failed_subtask_ids"] == [], "失败清空（已转放弃）"
    assert "st-4" not in r["dispatch_remaining"], "依赖被放弃者的 st-4 应移出 remaining（防死循环）"


def test_partial_delivery_escalates_when_no_completed():
    """0 完成子任务 → 不部分交付，仍 escalate（整任务失败，不假成功）。"""
    plan = TaskPlan(subtasks=[_st("st-1")])
    state = {
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": _wo("st-1", False)},
        "subtask_retry_counts": {"st-1": 3},
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes._get_brain_llm") as m:
        _patch_llm(m)
        r = _run(state)
    assert r.get("failure_escalated") is True, "无完成产出应 escalate，不得伪 PARTIAL"
    assert r["failure_strategy"] == "escalate"
    assert not r.get("abandoned_subtask_ids")


def test_refusal_routes_to_force_strong():
    """refusal_hard_fail 子任务重试（未到上限）→ 标记 force_strong（下轮走最强模型）。"""
    plan = TaskPlan(subtasks=[_st("st-1"), _st("st-2")])
    state = {
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _wo("st-1", True),
                            "st-2": _wo("st-2", False, decision_source="refusal_hard_fail")},
        "subtask_retry_counts": {},  # 未到上限 → 走 retry，携 force_strong
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes._get_brain_llm") as m:
        _patch_llm(m)
        r = _run(state)
    assert (r.get("subtask_force_strong") or {}).get("st-2") is True, \
        f"拒答子任务应标 force_strong: {r.get('subtask_force_strong')}"


def test_non_refusal_not_forced_strong():
    """普通编译失败(非拒答)不标 force_strong（只对 refusal_hard_fail 升级最强模型）。"""
    plan = TaskPlan(subtasks=[_st("st-1"), _st("st-2")])
    state = {
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _wo("st-1", True), "st-2": _wo("st-2", False)},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes._get_brain_llm") as m:
        _patch_llm(m)
        r = _run(state)
    assert not (r.get("subtask_force_strong") or {}).get("st-2"), "非拒答不应 force_strong"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
