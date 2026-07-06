#!/usr/bin/env python3
"""T-B（round28）：撞【云端 token 预算】硬闸后，抢救已完成子任务为 PARTIAL，不整单 FAILED 丢产物。

根因：token 闸门(store.check_task_token_limit) 在 dispatch/merge 等 9 个节点 end 触发
raise TaskTokenLimitExceeded → 旧路径冒泡到 run_task/resume 的泛 except → 无脑标 FAILED，
跳过 _handle_post_run 的 PARTIAL 组装 → 已 L1 通过、真实落盘/合并的子任务产物被整单丢弃
（round28 实测 4/55 完成度撞闸，丢弃完整 ruoyi-alarm 模块的真实文件）。

治本（与墙钟同理，但云端预算是【成本护栏】非交付失败，更不该连坐丢已产出工作）：
从 checkpointer 取当前 state，有已完成产物则诚实标 PARTIAL（列明因预算中止、余下未完成，
可重跑续做），无任何完成才仍 FAILED。本测试覆盖 _finalize_governor_partial（纯终态归一化，
给定 state）与 _salvage_partial_from_checkpoint（checkpoint 取不到时退回 FAILED）。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import runner


class _CaptureTopic:
    """_FanoutTopic 替身：捕获 publish 的事件供断言。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


def _plan(ids):
    return {"subtasks": [{"id": i} for i in ids]}


# ── _count_completed_in_plan：completed 语义单一事实源 ──────────────


def test_count_completed_in_plan_only_l1_passed_within_plan():
    state = {
        "plan": _plan(["st-1", "st-2", "st-3"]),
        "subtask_results": {
            "st-1": {"l1_passed": True},
            "st-2": {"l1_passed": False},   # 未过 L1 不算
            "st-3": {"l1_passed": True},
            "st-OLD": {"l1_passed": True},  # 不在当前 plan（replan 残留）不算
        },
    }
    assert runner._count_completed_in_plan(state) == 2
    print("  ✅ _count_completed_in_plan 只计当前 plan 内 L1 通过")


def test_count_completed_in_plan_empty():
    assert runner._count_completed_in_plan({}) == 0
    assert runner._count_completed_in_plan({"subtask_results": {}}) == 0
    print("  ✅ _count_completed_in_plan 无结果=0")


# ── _finalize_governor_partial：有完成→PARTIAL，无完成→FAILED ──────


def _run_finalize(state):
    topic = _CaptureTopic()
    calls = {}

    def fake_update(tid, **kw):
        calls.setdefault("status", kw.get("status"))
        if "status" in kw:
            calls["status"] = kw["status"]

    with patch.object(runner.store, "get_task", return_value={"description": "x", "project_id": "p"}), \
         patch.object(runner.store, "update_task", side_effect=fake_update), \
         patch.object(runner.store, "estimate_token_usage", return_value={"total": 1}), \
         patch.object(runner.store, "compute_task_duration_seconds", return_value=1.0), \
         patch.object(runner.store, "create_notification", return_value=None):
        status = asyncio.run(runner._finalize_governor_partial(
            "t-tb", state, topic,
            reason_code="token_limit_exceeded",
            reason_msg="云端 token 预算超限 (9/8)",
        ))
    return status, calls, topic.events


def test_completed_subtasks_salvaged_as_partial():
    """有 L1 通过的已完成子任务 → PARTIAL（非 FAILED），并发出结果 payload。"""
    state = {
        "plan": _plan(["st-1", "st-2"]),
        "subtask_results": {"st-1": {"l1_passed": True}, "st-2": {"l1_passed": True}},
        "merged_diff": "--- a\n+++ b\n",
        "l2_passed": True,
    }
    status, calls, events = _run_finalize(state)
    assert status == "PARTIAL", (status, calls)
    assert calls["status"] == "PARTIAL"
    steps = [e.get("step") for e in events]
    assert "complete" in steps, steps
    assert "result" in steps, steps
    # partial 事件须点明因预算中止 + 抢救数
    partial_evt = next(e for e in events if e.get("step") == "complete")
    assert partial_evt.get("status") == "partial"
    assert "token" in partial_evt.get("message", "").lower() or "预算" in partial_evt.get("message", "")
    print("  ✅ 有完成子任务→PARTIAL 抢救+结果 payload")


def test_no_completed_falls_back_failed():
    """无任何 L1 通过的完成子任务 → 仍 FAILED（无可抢救产物，不伪造 PARTIAL）。"""
    state = {
        "plan": _plan(["st-1", "st-2"]),
        "subtask_results": {"st-1": {"l1_passed": False}},
    }
    status, calls, events = _run_finalize(state)
    assert status == "FAILED", (status, calls)
    assert calls["status"] == "FAILED"
    steps = [e.get("step") for e in events]
    assert "error" in steps, steps
    assert "result" not in steps  # 无产物不发结果 payload
    print("  ✅ 无完成子任务→FAILED（不伪造 PARTIAL）")


def test_checkpoint_load_failure_falls_back_failed():
    """checkpoint 取不到 state（PG 抖动/无快照）→ 退回 FAILED（不比旧路径更差）。"""
    topic = _CaptureTopic()
    calls = {}

    async def _none(_tid):
        return None

    with patch.object(runner.store, "get_task", return_value={"description": "x", "project_id": "p"}), \
         patch.object(runner.store, "update_task", side_effect=lambda tid, **kw: calls.update(kw)), \
         patch.object(runner.store, "create_notification", return_value=None), \
         patch.object(runner, "_load_state_snapshot", side_effect=_none):
        asyncio.run(runner._salvage_partial_from_checkpoint(
            "t-tb", topic,
            reason_code="token_limit_exceeded",
            reason_msg="云端 token 预算超限",
        ))
    assert calls.get("status") == "FAILED", calls
    assert any(e.get("step") == "error" for e in topic.events)
    print("  ✅ checkpoint 取不到→FAILED 兜底")


def test_salvage_from_checkpoint_partial_when_state_has_completed():
    """checkpoint 取到含完成子任务的 state → 走 PARTIAL 抢救。"""
    topic = _CaptureTopic()
    calls = {}
    state = {
        "plan": _plan(["st-1"]),
        "subtask_results": {"st-1": {"l1_passed": True}},
        "merged_diff": "d",
    }

    async def _snap(_tid):
        return state

    with patch.object(runner.store, "get_task", return_value={"description": "x", "project_id": "p"}), \
         patch.object(runner.store, "update_task", side_effect=lambda tid, **kw: calls.update(kw)), \
         patch.object(runner.store, "estimate_token_usage", return_value={"total": 1}), \
         patch.object(runner.store, "compute_task_duration_seconds", return_value=1.0), \
         patch.object(runner.store, "create_notification", return_value=None), \
         patch.object(runner, "_load_state_snapshot", side_effect=_snap):
        asyncio.run(runner._salvage_partial_from_checkpoint(
            "t-tb", topic,
            reason_code="token_limit_exceeded",
            reason_msg="云端 token 预算超限",
        ))
    assert calls.get("status") == "PARTIAL", calls
    print("  ✅ checkpoint 取到完成态→PARTIAL")


if __name__ == "__main__":
    import inspect

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn) and not inspect.signature(fn).parameters:
            fn()
    print("\nT-B token PARTIAL 抢救单测通过。")
