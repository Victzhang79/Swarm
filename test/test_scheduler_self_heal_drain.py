#!/usr/bin/env python3
"""2nd#3 回归：调度器自愈排水——DB 权威源，队列丢失(Redis flap/内存清)不必等重启对账。

队列空+有空槽的 idle tick 节流触发：DB 里 SUBMITTED 但不在飞/不在跑 = 陈滞项 → 重入队。
纯 monkeypatch，无 DB/Redis 依赖。
"""

from __future__ import annotations


def _setup(monkeypatch, cands, running=()):
    import swarm.brain.scheduler as sched
    from swarm.project import store
    from swarm.infra.redis_client import TaskQueue

    enq: list = []
    monkeypatch.setattr(store, "list_orphan_candidates", lambda: cands)
    monkeypatch.setattr(TaskQueue, "enqueue",
                        staticmethod(lambda tid, pid, priority="normal": enq.append((tid, pid, priority))))
    monkeypatch.setattr(sched, "is_task_claimed", lambda tid: tid in running)
    monkeypatch.setattr(sched, "_is_already_running", lambda tid: tid in running)
    sched._pending_meta.clear()
    sched._inflight.clear()
    return sched, enq


def test_drain_reenqueues_stranded_submitted(monkeypatch):
    cands = [
        {"id": "t1", "project_id": "p", "description": "d1", "status": "SUBMITTED",
         "queue_priority": "urgent", "auto_accept": True},
    ]
    sched, enq = _setup(monkeypatch, cands)
    n = sched._drain_stranded_submitted()
    assert n == 1
    assert enq == [("t1", "p", "urgent")]
    # 回填 meta 便于出队时无需再查库
    assert sched._pending_meta["t1"]["description"] == "d1"
    assert sched._pending_meta["t1"]["auto_accept"] is True


def test_drain_skips_non_submitted(monkeypatch):
    """非 SUBMITTED（已开跑/审批认领）绝不重入队——交对账/resume 处置，不凭空双跑。"""
    cands = [
        {"id": "a", "project_id": "p", "description": "d", "status": "ANALYZING",
         "queue_priority": "normal"},
        {"id": "b", "project_id": "p", "description": "d", "status": "CONFIRMING",
         "queue_priority": "normal"},
        {"id": "c", "project_id": "p", "description": "d", "status": "DONE",
         "queue_priority": "normal"},
    ]
    sched, enq = _setup(monkeypatch, cands)
    assert sched._drain_stranded_submitted() == 0
    assert enq == []


def test_drain_skips_already_running(monkeypatch):
    """SUBMITTED 但已在飞/在跑（刚出队窗口）→ 跳过，不制造重复队列项。"""
    cands = [
        {"id": "t1", "project_id": "p", "description": "d", "status": "SUBMITTED",
         "queue_priority": "normal"},
    ]
    sched, enq = _setup(monkeypatch, cands, running={"t1"})
    assert sched._drain_stranded_submitted() == 0
    assert enq == []


def test_maybe_drain_throttled(monkeypatch):
    """节流：短间隔内二次调用不重复查库/排水。"""
    import swarm.brain.scheduler as sched

    calls = {"n": 0}
    monkeypatch.setattr(sched, "_drain_stranded_submitted", lambda: calls.__setitem__("n", calls["n"] + 1))
    sched._last_drain_ts = 0.0
    sched._maybe_drain_stranded()  # 首次触发
    first = calls["n"]
    sched._maybe_drain_stranded()  # 紧接第二次 → 被节流
    assert calls["n"] == first == 1


def test_loop_calls_drain_when_queue_empty():
    """_loop 在 dequeue 返 None（队列空+有空槽）时走自愈排水（源码守卫）。"""
    import inspect
    import swarm.brain.scheduler as sched

    src = inspect.getsource(sched.start_task_scheduler)
    assert "_maybe_drain_stranded()" in src, "_loop 队列空分支未接自愈排水（2nd#3 回归）"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
