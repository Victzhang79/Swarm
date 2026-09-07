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
    queued: set[str] = set()
    monkeypatch.setattr(store, "list_orphan_candidates", lambda: cands)
    def _enqueue(tid, pid, priority="normal"):
        enq.append((tid, pid, priority))
        queued.add(tid)

    monkeypatch.setattr(TaskQueue, "enqueue", staticmethod(_enqueue))
    monkeypatch.setattr(TaskQueue, "queued_task_ids", staticmethod(lambda: set(queued)))
    monkeypatch.setattr(sched, "is_task_claimed", lambda tid: tid in running)
    monkeypatch.setattr(sched, "_is_already_running", lambda tid: tid in running)
    sched._pending_meta.clear()
    sched._inflight.clear()
    return sched, enq


async def test_drain_reenqueues_stranded_submitted(monkeypatch):
    cands = [
        {"id": "t1", "project_id": "p", "description": "d1", "status": "SUBMITTED",
         "queue_priority": "urgent", "auto_accept": True},
    ]
    sched, enq = _setup(monkeypatch, cands)
    n = await sched._drain_stranded_submitted()
    assert n == 1
    assert enq == [("t1", "p", "urgent")]
    assert sched._pending_meta["t1"]["project_id"] == "p"


async def test_repeated_full_load_drain_does_not_duplicate_pending_queue_item(monkeypatch):
    cands = [
        {"id": "t1", "project_id": "p", "description": "d", "status": "SUBMITTED",
         "queue_priority": "normal", "auto_accept": False},
    ]
    sched, enq = _setup(monkeypatch, cands)

    assert await sched._drain_stranded_submitted(known_empty=False) == 1
    assert await sched._drain_stranded_submitted(known_empty=False) == 0
    assert enq == [("t1", "p", "normal")]


async def test_known_empty_drain_repairs_lost_queue_even_with_pending_meta(monkeypatch):
    cands = [
        {"id": "t1", "project_id": "p", "description": "d", "status": "SUBMITTED",
         "queue_priority": "normal", "auto_accept": False},
    ]
    sched, enq = _setup(monkeypatch, cands)
    sched._pending_meta["t1"] = {"project_id": "p", "description": "d", "auto_accept": False}

    assert await sched._drain_stranded_submitted(known_empty=True) == 1
    assert enq == [("t1", "p", "normal")]


async def test_full_load_repairs_partial_queue_loss_despite_stale_pending_meta(monkeypatch):
    """其它流量令队列永不空时，丢失项不能被残留 meta 永久遮住。"""
    cands = [
        {"id": "lost", "project_id": "p", "description": "d", "status": "SUBMITTED",
         "queue_priority": "normal", "auto_accept": False},
    ]
    sched, enq = _setup(monkeypatch, cands)
    sched._pending_meta["lost"] = {
        "project_id": "p", "description": "d", "auto_accept": False,
    }

    assert await sched._drain_stranded_submitted(known_empty=False) == 1
    assert await sched._drain_stranded_submitted(known_empty=False) == 0
    assert enq == [("lost", "p", "normal")]


async def test_drain_skips_non_submitted(monkeypatch):
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
    assert await sched._drain_stranded_submitted() == 0
    assert enq == []


async def test_drain_skips_already_running(monkeypatch):
    """SUBMITTED 但已在飞/在跑（刚出队窗口）→ 跳过，不制造重复队列项。"""
    cands = [
        {"id": "t1", "project_id": "p", "description": "d", "status": "SUBMITTED",
         "queue_priority": "normal"},
    ]
    sched, enq = _setup(monkeypatch, cands, running={"t1"})
    assert await sched._drain_stranded_submitted() == 0
    assert enq == []


async def test_drain_per_record_guard_continues_on_enqueue_error(monkeypatch):
    """对抗复核 F2：某条 enqueue 抛错（Redis flap 中途）不弃其余陈滞项。"""
    import swarm.brain.scheduler as sched
    from swarm.project import store
    from swarm.infra.redis_client import TaskQueue

    cands = [
        {"id": "bad", "project_id": "p", "description": "d", "status": "SUBMITTED", "queue_priority": "normal"},
        {"id": "good", "project_id": "p", "description": "d", "status": "SUBMITTED", "queue_priority": "normal"},
    ]
    ok: list = []

    def _enq(tid, pid, priority="normal"):
        if tid == "bad":
            raise ConnectionError("redis flap")
        ok.append(tid)

    monkeypatch.setattr(store, "list_orphan_candidates", lambda: cands)
    monkeypatch.setattr(TaskQueue, "enqueue", staticmethod(_enq))
    monkeypatch.setattr(sched, "_is_already_running", lambda tid: False)
    sched._inflight.clear()
    n = await sched._drain_stranded_submitted()
    assert n == 1 and ok == ["good"], "bad 抛错后 good 仍被重入队（F2）"


async def test_maybe_drain_throttled(monkeypatch):
    """节流：短间隔内二次调用不重复查库/排水。"""
    import swarm.brain.scheduler as sched

    calls = {"n": 0}

    async def _fake(*, known_empty=True):
        calls["n"] += 1

    monkeypatch.setattr(sched, "_drain_stranded_submitted", _fake)
    sched._last_drain_ts = 0.0
    await sched._maybe_drain_stranded()  # 首次触发
    first = calls["n"]
    await sched._maybe_drain_stranded()  # 紧接第二次 → 被节流
    assert calls["n"] == first == 1


async def test_loop_calls_drain_when_queue_empty(monkeypatch):
    """真实消费循环在 dequeue 返 None 时以“已知队列空”模式触发排水。"""
    import asyncio
    import swarm.brain.scheduler as sched

    seen: list[bool] = []
    drained = asyncio.Event()

    async def _spy(*, known_empty=True):
        seen.append(known_empty)
        if known_empty:
            drained.set()

    # 乱序鲁棒：全量套件中前面的 DB 集成测试会把 redis_client 全局缓存成真连接，
    # 消费循环改走 dequeue_blocking（BLPOP 真 Redis 每 tick 阻塞 2s，本测试只 patch 了
    # 非阻塞 dequeue）→ 2s wait_for 内触达不到排水分支。强制内存后端确定性走 patched 路径
    # （与 test_scheduler.py 的 _force_memory_backend 同一处方的单测版）。
    monkeypatch.setattr("swarm.infra.redis_client.get_redis", lambda: None)
    monkeypatch.setattr(sched, "_maybe_drain_stranded", _spy)
    monkeypatch.setattr(sched.TaskQueue, "dequeue", lambda **_kwargs: None)
    assert not sched.is_consumer_running()
    saved_inflight = set(sched._inflight)
    sched._inflight.clear()
    try:
        await sched.start_task_scheduler()
        try:
            await asyncio.wait_for(drained.wait(), timeout=2.0)
        finally:
            await sched.stop_task_scheduler()
    finally:
        sched._inflight.clear()
        sched._inflight.update(saved_inflight)

    assert True in seen


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
