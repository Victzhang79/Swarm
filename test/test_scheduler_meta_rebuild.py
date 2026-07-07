#!/usr/bin/env python3
"""P0-A：调度器出队 meta 从 DB 重建 + 去重守卫单测。

修前：leader 重启后 Redis 队列存活但进程内 _pending_meta 清零 → dequeue 拿到 task 但 meta=None
→ `continue` 静默丢，任务永久卡 SUBMITTED（既不跑也不失败）。注释谎称"由 orphan 恢复逻辑处理"。
修后：meta 缺失时从 DB get_task 重建（陈旧/已终态才丢弃）；同任务已在跑/在飞时去重丢弃防双跑。

纯逻辑，monkeypatch store.get_task / runner.is_task_running，不依赖真 DB/Redis。
"""

from __future__ import annotations

import swarm.brain.scheduler as sched


def _reset():
    sched._pending_meta.clear()
    sched._inflight.clear()


def test_resolve_uses_in_memory_meta_when_present(monkeypatch):
    """D40 后语义：缓存命中返回缓存 meta，但 DB status 复核（只认 SUBMITTED）对缓存路径同样生效。"""
    _reset()
    from swarm.project import store

    monkeypatch.setattr(store, "get_task", lambda tid: {
        "id": tid, "project_id": "p1", "description": "d", "status": "SUBMITTED",
    })
    sched._pending_meta["t1"] = {"project_id": "p1", "description": "d", "auto_accept": True}
    meta = sched._resolve_exec_meta("t1")
    assert meta == {"project_id": "p1", "description": "d", "auto_accept": True}


def test_resolve_rebuilds_from_db_when_meta_lost(monkeypatch):
    """模拟重启：_pending_meta 空，从 DB 重建执行参数（含持久化的 auto_accept）。"""
    _reset()
    from swarm.project import store

    monkeypatch.setattr(store, "get_task", lambda tid: {
        "id": tid, "project_id": "pX", "description": "rebuild me",
        "status": "SUBMITTED", "auto_accept": True, "queue_priority": "urgent",
    })
    meta = sched._resolve_exec_meta("lost1")
    assert meta == {"project_id": "pX", "description": "rebuild me", "auto_accept": True}
    # 回填缓存，避免二次查库
    assert sched._pending_meta["lost1"]["project_id"] == "pX"


def test_resolve_discards_stale_terminal_task(monkeypatch):
    """DB 里已终态（如队列残留 + 任务已被取消）→ 返回 None → 出队丢弃，不重跑。"""
    _reset()
    from swarm.project import store

    for term in ("DONE", "FAILED", "CANCELLED", "PARTIAL"):
        monkeypatch.setattr(store, "get_task", lambda tid, s=term: {
            "id": tid, "project_id": "p", "description": "d", "status": s,
        })
        assert sched._resolve_exec_meta("stale") is None
        assert "stale" not in sched._pending_meta


def test_resolve_discards_missing_task(monkeypatch):
    """DB 无记录（任务已删）→ None → 丢弃。"""
    _reset()
    from swarm.project import store

    monkeypatch.setattr(store, "get_task", lambda tid: None)
    assert sched._resolve_exec_meta("ghost") is None


def test_resolve_fail_closed_on_non_submitted_in_queue(monkeypatch):
    """P0 治本：队列唯一合法待跑项是 SUBMITTED。任何【已开跑/审批认领后】的活跃态
    （ANALYZING/MONITORING…）或中断挂起态/终态残留项 → 不重建、不另起 run_task
    （否则会用全新 initial_state 在同 thread_id 上与既有 checkpoint 双跑）。"""
    _reset()
    from swarm.project import store

    for bad in ("ANALYZING", "MONITORING", "DISPATCHING", "MERGING",
                "CONFIRMING", "DELIVERING", "CLARIFYING", "DESIGN_REVIEW", "POOLED", "WEIRD"):
        monkeypatch.setattr(store, "get_task", lambda tid, s=bad: {
            "id": tid, "project_id": "p", "description": "d", "status": s,
        })
        assert sched._resolve_exec_meta("polluted") is None, bad
        assert "polluted" not in sched._pending_meta


def test_dedup_guard_skips_when_inflight():
    _reset()
    sched._inflight.add("running1")
    assert sched._is_already_running("running1") is True


def test_dedup_guard_skips_when_runner_reports_running(monkeypatch):
    _reset()
    from swarm.brain import runner

    monkeypatch.setattr(runner, "is_task_running", lambda tid: tid == "r2")
    assert sched._is_already_running("r2") is True
    assert sched._is_already_running("other") is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
