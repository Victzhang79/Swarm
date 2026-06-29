#!/usr/bin/env python3
"""brain/runner.py 真实单测。

聚焦无需真实 Brain 执行的纯逻辑：任务状态判断（is_running/orphaned/can_retry）、
SSE 队列注册与清理、通知钩子 _emit_task_notification、结果 payload 构造。
DB 访问统一 mock store.get_task / store.create_notification。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import runner


def _reset_runner_state():
    runner._task_running.clear()
    runner._task_queues.clear()
    runner._task_handles.clear()


# ── SSE 队列注册 / 清理 ──────────────────────────


def test_register_and_get_task_queue():
    _reset_runner_state()
    q = runner.register_task_queue("task-1")
    assert runner.get_task_queue("task-1") is q
    assert runner.get_task_queue("nonexistent") is None
    print("  ✅ register/get_task_queue")


def test_cleanup_old_queues_keeps_running():
    _reset_runner_state()
    # 先注册并标记一个 running 队列，再灌入大量队列触发清理
    runner.register_task_queue("keep-me")
    runner._task_running.add("keep-me")
    for i in range(250):
        runner.register_task_queue(f"t-{i}")
    # register 内部已反复触发 _cleanup_old_queues；running 的不会被清理
    assert "keep-me" in runner._task_queues
    assert len(runner._task_queues) <= 200
    _reset_runner_state()
    print("  ✅ _cleanup_old_queues 保留 running + 限长")


# ── is_task_running ──────────────────────────────


def test_is_task_running():
    _reset_runner_state()
    assert runner.is_task_running("task-x") is False
    runner._task_running.add("task-x")
    assert runner.is_task_running("task-x") is True
    _reset_runner_state()
    print("  ✅ is_task_running")


# ── is_task_orphaned ─────────────────────────────


def test_is_task_orphaned_true():
    """DB 活跃状态 + 本进程未跑 → orphaned。"""
    _reset_runner_state()
    with patch.object(runner.store, "get_task", return_value={"status": "DISPATCHING"}):
        assert runner.is_task_orphaned("task-1") is True
    print("  ✅ is_task_orphaned 活跃态+未跑=True")


def test_is_task_orphaned_false_when_running():
    _reset_runner_state()
    runner._task_running.add("task-1")
    with patch.object(runner.store, "get_task", return_value={"status": "DISPATCHING"}):
        assert runner.is_task_orphaned("task-1") is False
    _reset_runner_state()
    print("  ✅ is_task_orphaned 正在跑=False")


def test_is_task_orphaned_false_when_terminal():
    _reset_runner_state()
    with patch.object(runner.store, "get_task", return_value={"status": "DONE"}):
        assert runner.is_task_orphaned("task-1") is False
    print("  ✅ is_task_orphaned 终态=False")


def test_is_task_orphaned_no_task():
    _reset_runner_state()
    with patch.object(runner.store, "get_task", return_value=None):
        assert runner.is_task_orphaned("ghost") is False
    print("  ✅ is_task_orphaned 任务不存在=False")


# ── can_retry_task ───────────────────────────────


def test_can_retry_terminal_states():
    _reset_runner_state()
    # PARTIAL（部分交付终态）也必须可重跑——否则放弃的子任务永久卡死无法再试。
    for status in ("FAILED", "CANCELLED", "DONE", "PARTIAL"):
        with patch.object(runner.store, "get_task", return_value={"status": status}):
            ok, reason = runner.can_retry_task("t")
            assert ok is True, f"{status} 应可重跑"
            assert reason == ""
    print("  ✅ can_retry_task 终态(FAILED/CANCELLED/DONE/PARTIAL)可重跑")


def test_can_retry_running_rejected():
    _reset_runner_state()
    runner._task_running.add("t")
    with patch.object(runner.store, "get_task", return_value={"status": "DISPATCHING"}):
        ok, reason = runner.can_retry_task("t")
    assert ok is False
    assert "正在执行" in reason
    _reset_runner_state()
    print("  ✅ can_retry_task 执行中拒绝")


def test_can_retry_review_states_rejected():
    _reset_runner_state()
    for status in ("DELIVERING", "CONFIRMING"):
        with patch.object(runner.store, "get_task", return_value={"status": status}):
            ok, reason = runner.can_retry_task("t")
            assert ok is False
            assert "人工审核" in reason
    print("  ✅ can_retry_task 待审核态拒绝")


def test_can_retry_orphaned_allowed():
    """DB 活跃但本进程未跑（orphaned）→ 允许重跑。"""
    _reset_runner_state()
    with patch.object(runner.store, "get_task", return_value={"status": "MONITORING"}):
        ok, reason = runner.can_retry_task("t")
    assert ok is True
    assert reason == ""
    print("  ✅ can_retry_task orphaned 活跃态可重跑")


def test_can_retry_no_task():
    _reset_runner_state()
    with patch.object(runner.store, "get_task", return_value=None):
        ok, reason = runner.can_retry_task("ghost")
    assert ok is False
    assert "不存在" in reason
    print("  ✅ can_retry_task 任务不存在")


# ── _emit_task_notification ──────────────────────


def test_emit_notification_done():
    captured = {}

    def fake_create(etype, **kw):
        captured["etype"] = etype
        captured.update(kw)

    with patch.object(runner.store, "create_notification", side_effect=fake_create):
        runner._emit_task_notification(
            "abcdef123456", {"description": "修复登录 bug", "project_id": "p1"}, "DONE"
        )
    assert captured["etype"] == "task_completed"
    assert captured["title"] == "任务已完成"
    assert captured["task_id"] == "abcdef123456"
    assert captured["project_id"] == "p1"
    assert "abcdef12" in captured["message"]  # task_id 前 8 位
    print("  ✅ _emit_task_notification DONE→task_completed")


def test_emit_notification_failed():
    captured = {}
    with patch.object(
        runner.store, "create_notification",
        side_effect=lambda etype, **kw: captured.update({"etype": etype, **kw}),
    ):
        runner._emit_task_notification("t1", {"description": "x"}, "FAILED")
    assert captured["etype"] == "task_failed"
    assert captured["title"] == "任务失败"
    print("  ✅ _emit_task_notification FAILED→task_failed")


def test_emit_notification_swallows_errors():
    """通知写入失败不能影响主流程（吞异常）。"""
    with patch.object(runner.store, "create_notification", side_effect=RuntimeError("db down")):
        # 不应抛出
        runner._emit_task_notification("t1", {"description": "x"}, "DONE")
    print("  ✅ _emit_task_notification 写入失败不抛异常")


# ── _build_result_payload ────────────────────────


def test_build_result_payload_filters_empty():
    state = {
        "merged_diff": "--- a\n+++ b\n",
        "l2_passed": True,
        "complexity": "",          # 空串应过滤
        "plan": {},                # 空 dict 应过滤
        "learn_summary": None,     # None 应过滤
        "l3_skipped": False,       # bool False 保留
        "irrelevant": "x",         # 不在白名单应忽略
    }
    payload = runner._build_result_payload(state)
    assert payload["merged_diff"] == "--- a\n+++ b\n"
    assert payload["l2_passed"] is True
    assert payload["l3_skipped"] is False
    assert "complexity" not in payload
    assert "plan" not in payload
    assert "learn_summary" not in payload
    assert "irrelevant" not in payload
    print("  ✅ _build_result_payload 过滤空值/白名单外字段，保留 bool")


def test_build_result_payload_model_dump():
    """带 model_dump 的对象应被序列化。"""
    class FakeModel:
        def model_dump(self, mode="json"):
            return {"k": "v"}

    payload = runner._build_result_payload({"plan": FakeModel()})
    assert payload["plan"] == {"k": "v"}
    print("  ✅ _build_result_payload 调用 model_dump 序列化")


if __name__ == "__main__":
    import inspect

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                continue
            fn()
    print("\nrunner 单测通过。")
