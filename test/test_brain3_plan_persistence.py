"""A4 / brain#3：handle_failure 就地改 plan 的 checkpoint 持久化。

handle_failure 会就地修改 state["plan"] 的 SubTask（retry_guidance / pom 写权 / scope）。
这些改动只有随返回 dict 的 "plan" channel 写回才被 LangGraph checkpoint 持久化；否则
resume 后 plan 回滚、诊断全丢。外层 handle_failure 包装：凡再派发(dispatch_remaining)的
返回统一回传当前 plan。这里在 _handle_failure_impl 的 seam 上锁住该机制（行为测试，不断言
内部结构）。
"""
from __future__ import annotations

import swarm.brain.nodes as nodes
from swarm.types import FileScope, SubTask, TaskPlan


def _plan() -> TaskPlan:
    return TaskPlan(subtasks=[SubTask(id="st-1", description="x", scope=FileScope(create_files=["a/A.java"]))])


async def test_wrapper_injects_plan_on_redispatch(monkeypatch):
    # impl 走再派发分支但（如原 8 个返回中的多数）没带 plan → 包装回传当前(就地改的)plan
    plan = _plan()

    async def _impl(state):
        return {"dispatch_remaining": ["st-1"], "failure_strategy": "retry"}

    monkeypatch.setattr(nodes, "_handle_failure_impl", _impl)
    out = await nodes.handle_failure({"plan": plan})
    assert out["plan"] is plan


async def test_wrapper_no_plan_when_not_redispatch(monkeypatch):
    # escalate/replan 等非再派发返回 → 不注入 plan（replan 会重生成 plan，回传旧 plan 反而错）
    plan = _plan()

    async def _impl(state):
        return {"failure_strategy": "escalate", "l2_passed": False}

    monkeypatch.setattr(nodes, "_handle_failure_impl", _impl)
    out = await nodes.handle_failure({"plan": plan})
    assert "plan" not in out


async def test_wrapper_does_not_override_impl_plan(monkeypatch):
    # impl 自带 plan（如 _targeted_redecompose 的 new_plan）→ 不被覆盖
    plan_a, plan_b = _plan(), _plan()

    async def _impl(state):
        return {"dispatch_remaining": ["x"], "plan": plan_b}

    monkeypatch.setattr(nodes, "_handle_failure_impl", _impl)
    out = await nodes.handle_failure({"plan": plan_a})
    assert out["plan"] is plan_b


async def test_wrapper_tolerates_missing_plan(monkeypatch):
    # state 无 plan（防御）→ 不崩、不注入
    async def _impl(state):
        return {"dispatch_remaining": ["x"], "failure_strategy": "retry"}

    monkeypatch.setattr(nodes, "_handle_failure_impl", _impl)
    out = await nodes.handle_failure({})
    assert "plan" not in out
