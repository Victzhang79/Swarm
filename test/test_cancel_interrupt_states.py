#!/usr/bin/env python3
"""P0-D：CLARIFYING/DESIGN_REVIEW 人工闸死区修复单测。

修前：runner._ACTIVE_DB_STATUSES 缺 CLARIFYING/DESIGN_REVIEW → is_task_orphaned 对二者返 False
     → cancel/delete gate（status in _ACTIVE_DB_STATUSES and not is_task_orphaned）落 409 死区，
     且这俩态本进程崩溃后无法取消/删除（人工闸卡死）。
修后：SSOT 并集纳入这俩态 → orphaned 时 is_task_orphaned 返 True，cancel/delete 放行；
     can_retry_task 对称拦截全部中断挂起态（先答闸再谈重跑）。

cancel 端点 gate: `if not is_task_running and not is_task_orphaned: raise 409`
→ is_task_orphaned=True 即绕过 409 放行。故验证底层谓词即证明死区已修。
monkeypatch store.get_task，不依赖 DB。
"""

from __future__ import annotations

import swarm.brain.runner as runner


def _patch_task(monkeypatch, status):
    from swarm.project import store

    monkeypatch.setattr(store, "get_task", lambda tid: {"id": tid, "status": status})
    runner._task_running.discard("t")


def test_interrupt_states_now_in_active_db_statuses():
    for s in ("CLARIFYING", "DESIGN_REVIEW", "CONFIRMING", "DELIVERING"):
        assert s in runner._ACTIVE_DB_STATUSES


def test_orphaned_clarifying_is_detected_as_orphan(monkeypatch):
    _patch_task(monkeypatch, "CLARIFYING")
    # 修前：False（死区）；修后：True（可 cancel/delete）
    assert runner.is_task_orphaned("t") is True


def test_orphaned_design_review_is_detected_as_orphan(monkeypatch):
    _patch_task(monkeypatch, "DESIGN_REVIEW")
    assert runner.is_task_orphaned("t") is True


def test_cancel_gate_passes_for_orphaned_interrupt_states(monkeypatch):
    """复刻 cancel 端点 gate：not running and not orphaned → 409。orphaned=True 即放行。"""
    for s in ("CLARIFYING", "DESIGN_REVIEW"):
        _patch_task(monkeypatch, s)
        running = runner.is_task_running("t")
        orphaned = runner.is_task_orphaned("t")
        would_409 = (not running) and (not orphaned)
        assert would_409 is False, f"{s} 仍落 409 死区"


def test_retry_blocks_all_interrupt_suspended_states(monkeypatch):
    from swarm.project import store

    for s in ("CONFIRMING", "DELIVERING", "CLARIFYING", "DESIGN_REVIEW"):
        monkeypatch.setattr(store, "get_task", lambda tid, st=s: {"id": tid, "status": st})
        runner._task_running.discard("t")
        allowed, reason = runner.can_retry_task("t")
        assert allowed is False, f"{s} 不应允许直接重跑（须先答人工闸）"
        assert "人工审核" in reason


def test_notifications_cover_all_interrupt_states():
    """F2：CLARIFYING/DESIGN_REVIEW 也须进通知集 + 归类 waiting_review + 文案「待审核」，
    否则新增人工闸态用户收不到通知 → 静默死等（与 cancel 死区同源的第二个死区）。"""
    from swarm.project import store
    from swarm.task_states import INTERRUPT_SUSPENDED_STATES

    assert set(INTERRUPT_SUSPENDED_STATES) <= set(store._NOTIFY_STATUSES)
    assert {"DONE", "FAILED"} <= set(store._NOTIFY_STATUSES)
    for s in ("CLARIFYING", "DESIGN_REVIEW", "CONFIRMING", "DELIVERING"):
        assert store._task_event_type(s) == "waiting_review", s
        assert store._notification_message(s, "x").startswith("待审核"), s


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
