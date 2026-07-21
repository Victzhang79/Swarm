"""B9 前端/可观测性深读治本（11_frontend F3 = #106）行为级测试。

#106 进度聚合端点 get_task_progress 的【单一权威口径】——与 MONITOR 节点逐字一致，
从 checkpoint state 计数，绝不解析日志。#104/#105 是纯前端 JS（tasks.js），无 pytest 覆盖，
靠 findings 文档记录 + 手工核验。
"""
from __future__ import annotations

import asyncio

import pytest


class _FakeSnap:
    def __init__(self, values):
        self.values = values


class _FakeGraph:
    def __init__(self, values):
        self._v = values

    async def aget_state(self, config):
        return _FakeSnap(self._v)


def _patch_runner(monkeypatch, state):
    import swarm.brain.runner as runner
    import swarm.tracing as tracing
    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _FakeGraph(state))
    monkeypatch.setattr(runner.store, "get_task", lambda tid: {})
    monkeypatch.setattr(tracing, "brain_graph_config", lambda **kw: {})
    return runner


_STATE = {
    "dispatch_remaining": ["s5", "s6"],            # 剩余 2
    "subtask_results": {
        "s1": {"l1_passed": True},
        "s2": {"l1_passed": True},
        "s3": {"l1_passed": False},                # 滞留失败结果——不算完成（L1 过口径）
    },
    "failed_subtask_ids": ["s3"],                  # 失败 1
    "abandoned_subtask_ids": ["s4"],
    "give_up_isolated_ids": ["s4", "s7"],          # 放弃 = {s4,s7} → 2（去重）
    "plan": {"subtasks": [{"id": f"s{i}"} for i in range(1, 8)]},  # total 7
}


def test_106_progress_authoritative_counts(monkeypatch):
    runner = _patch_runner(monkeypatch, _STATE)
    p = asyncio.run(runner.get_task_progress("t1"))
    assert p["remaining"] == 2
    assert p["completed"] == 2      # s1,s2（s3 L1 未过不计）
    assert p["failed"] == 1
    assert p["abandoned"] == 2      # {s4,s7} 去重
    assert p["total"] == 7


def test_106_matches_monitor_formula(monkeypatch):
    """口径必须与 MONITOR 节点逐字一致（同一公式重算比对，防第三口径漂移）。"""
    from swarm.brain.nodes.shared import completed_l1_ids
    runner = _patch_runner(monkeypatch, _STATE)
    p = asyncio.run(runner.get_task_progress("t1"))
    # MONITOR: 剩余/已完成(L1过)/失败/放弃(abandoned∪give_up)
    assert p["remaining"] == len(_STATE["dispatch_remaining"])
    assert p["completed"] == len(completed_l1_ids(_STATE["subtask_results"]))
    assert p["failed"] == len(_STATE["failed_subtask_ids"])
    assert p["abandoned"] == len(
        set(_STATE["abandoned_subtask_ids"]) | set(_STATE["give_up_isolated_ids"]))


def test_106_subtask_detail_from_same_sets(monkeypatch):
    runner = _patch_runner(monkeypatch, _STATE)
    p = asyncio.run(runner.get_task_progress("t1"))
    st = {s["id"]: s["status"] for s in p["subtasks"]}
    assert st == {
        "s1": "done", "s2": "done", "s3": "failed", "s4": "abandoned",
        "s5": "pending", "s6": "pending", "s7": "abandoned",
    }


def test_106_real_taskplan_object_no_crash(monkeypatch):
    """★对抗复核 HIGH 回归★：checkpoint 里 plan 是 TaskPlan Pydantic 实例（非 dict）——
    get_task_progress 必须 model_dump 后再取 subtasks，绝不能 plan.get() AttributeError→500。
    dict fixture 掩盖了这个真 500，此用例用真 TaskPlan 对象锁死。"""
    from swarm.types import FileScope, SubTask, TaskPlan
    real_plan = TaskPlan(subtasks=[
        SubTask(id=f"s{i}", description="d",
                scope=FileScope(writable=[], readable=[], create_files=[]))
        for i in range(1, 8)
    ])
    state = dict(_STATE)
    state["plan"] = real_plan  # 真 Pydantic 对象，非 dict
    runner = _patch_runner(monkeypatch, state)
    p = asyncio.run(runner.get_task_progress("t1"))
    assert p is not None and p["total"] == 7
    st = {s["id"]: s["status"] for s in p["subtasks"]}
    assert st["s1"] == "done" and st["s3"] == "failed" and st["s5"] == "pending"


def test_106_no_checkpoint_returns_none(monkeypatch):
    runner = _patch_runner(monkeypatch, None)  # values=None → 无态
    assert asyncio.run(runner.get_task_progress("t1")) is None


def test_106_empty_state_returns_none(monkeypatch):
    runner = _patch_runner(monkeypatch, {})     # 空态 → None
    assert asyncio.run(runner.get_task_progress("t1")) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
