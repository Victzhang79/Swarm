"""阶段1.5（§九 TaskLedger）：所有重试层发起前统一从 ledger 扣减 — 行为测试。

  - handle_failure 入口：预算耗尽不再开任何恢复轮（retry/换模型/replan 都烧钱），
    确定性抛 TaskTokenLimitExceeded → runner salvage→PARTIAL。
  - plan()：预算异常绝不降级成兜底假计划（把"没钱"伪装成"规划失败"继续跑）。
  - ULTRA 分批 attempt 循环：预算异常绝不吞成"模块失败"记账（那会降级跳过后
    继续烧兄弟批）。
  - _invoke_llm_abortable：主备切换前查余额，耗尽不再对备用烧全款。
"""

from __future__ import annotations

import asyncio

import pytest

import swarm.brain.nodes as nodes
from swarm.brain.nodes import _invoke_llm_abortable, handle_failure, plan
from swarm.models import ledger, usage_tracker
from swarm.models.errors import TaskTokenLimitExceeded
from swarm.types import Complexity, FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    usage_tracker.set_current_task(None)
    yield
    usage_tracker.set_current_task(None)
    ledger._reset_for_tests()


def _exhaust(task_id: str, budget: int = 10_000):
    ledger.attach(task_id, budget_total=budget)
    rid = ledger.reserve(task_id, est_in=budget, est_out=0, kind="cloud")
    ledger.settle(rid, real_in=budget, real_out=0)


class _TokenLimitLLM:
    """模拟 _LedgerGuard 在调用发起点拒绝（预算耗尽）。"""

    async def ainvoke(self, messages):
        raise TaskTokenLimitExceeded({"task_id": "x", "total": 1, "limit_effective": 1})


def test_handle_failure_entry_gates_on_budget():
    _exhaust("r1")
    state = {
        "task_id": "r1",
        "complexity": Complexity.MEDIUM,
        "plan": TaskPlan(subtasks=[SubTask(id="st-1", description="d",
                                           difficulty=SubTaskDifficulty.MEDIUM,
                                           scope=FileScope(writable=["a"]))],
                         parallel_groups=[["st-1"]]),
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": WorkerOutput(subtask_id="st-1", diff="", summary="x",
                                                 l1_passed=False)},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
    }
    with pytest.raises(TaskTokenLimitExceeded):
        asyncio.run(handle_failure(state))


async def test_plan_reraises_token_limit_not_degrade(monkeypatch):
    """预算异常必须穿透 plan() 的降级兜底（否则伪装成 plan_generation_failed 继续跑）。"""
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _TokenLimitLLM())
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    with pytest.raises(TaskTokenLimitExceeded):
        await plan({
            "task_description": "t",
            "complexity": Complexity.MEDIUM,
        })


async def test_plan_ultra_batch_reraises_token_limit(monkeypatch):
    """ULTRA 分批：attempt 循环里预算异常绝不吞成模块 error（任务级 salvage）。"""
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _TokenLimitLLM())
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    with pytest.raises(TaskTokenLimitExceeded):
        await plan({
            "task_description": "big",
            "complexity": Complexity.ULTRA,
            "tech_design_file_plan": [{"path": f"m/f{i}.txt", "action": "create"}
                                      for i in range(40)],
        })


async def test_abortable_fallback_gated_by_budget():
    """primary 超时 + 预算耗尽 → 切备前确定性抛，不再对备用烧全款。"""
    _exhaust("r2")
    usage_tracker.set_current_task("r2")

    class _SlowLLM:  # 无 astream → wait_for(ainvoke) 超时路径
        async def ainvoke(self, messages):
            await asyncio.sleep(1.0)

    fallback_called: list = []

    class _Fallback:
        async def ainvoke(self, messages):
            fallback_called.append(1)

    with pytest.raises(TaskTokenLimitExceeded):
        await _invoke_llm_abortable(_SlowLLM(), [], 0.05, _Fallback())
    assert not fallback_called, "预算耗尽仍烧备用=对最贵形态失明"


async def test_abortable_fallback_allowed_with_budget():
    """有预算时主备切换行为不变（回归锚点）。"""
    ledger.attach("r3", budget_total=1_000_000)
    usage_tracker.set_current_task("r3")

    class _SlowLLM:
        async def ainvoke(self, messages):
            await asyncio.sleep(1.0)

    class _Fallback:
        async def ainvoke(self, messages):
            class _R:
                content = "ok"
            return _R()

    r = await _invoke_llm_abortable(_SlowLLM(), [], 0.05, _Fallback())
    assert r.content == "ok"
