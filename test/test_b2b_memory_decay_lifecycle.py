"""Batch 2-B：leader 独占 MemoryStore 的连接所有权行为锁。"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock

import pytest


class FakeMemoryStore:
    instances: list["FakeMemoryStore"] = []
    connect_error: Exception | None = None

    def __init__(self):
        self.connect = AsyncMock(side_effect=self.connect_error)
        self.close = AsyncMock()
        self.instances.append(self)


async def _capture_owned_decay_coro(monkeypatch, decay_impl, *, connect_error=None):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.memory.decay as decay_mod
    import swarm.memory.store as store_mod

    FakeMemoryStore.instances.clear()
    FakeMemoryStore.connect_error = connect_error

    class FakeDecay:
        def __init__(self, store):
            self.store = store

        async def start_daily_decay(self):
            return await decay_impl()

    captured = []
    monkeypatch.setattr(store_mod, "MemoryStore", FakeMemoryStore)
    monkeypatch.setattr(decay_mod, "MemoryDecay", FakeDecay)
    monkeypatch.setattr(app_mod, "_spawn_bg", lambda coro: captured.append(coro))
    await app_mod._start_memory_decay_scheduler()
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_memory_decay_normal_exit_closes_owned_connection_once(monkeypatch):
    async def finishes():
        return None

    owned = await _capture_owned_decay_coro(monkeypatch, finishes)
    await owned

    store = FakeMemoryStore.instances[0]
    store.connect.assert_awaited_once()
    store.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_decay_exception_closes_once_and_is_observed(monkeypatch, caplog):
    from swarm.infra.degrade import degrade_counts

    before = degrade_counts().get("memory.decay.scheduler_error", 0)
    async def fails():
        raise RuntimeError("decay exploded")

    owned = await _capture_owned_decay_coro(monkeypatch, fails)
    await owned

    store = FakeMemoryStore.instances[0]
    store.close.assert_awaited_once()
    assert "decay exploded" in caplog.text
    assert degrade_counts().get("memory.decay.scheduler_error", 0) == before + 1


@pytest.mark.asyncio
async def test_memory_decay_leadership_loss_cancels_and_closes_once(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler
    import swarm.knowledge.scheduler as kb_scheduler

    started = asyncio.Event()

    async def waits_forever():
        started.set()
        await asyncio.Future()

    # 本用例需要真实 _spawn_bg，故只借 helper 的 fake 类装配，不保留其 capture patch。
    import swarm.memory.decay as decay_mod
    import swarm.memory.store as store_mod

    FakeMemoryStore.instances.clear()

    class FakeDecay:
        def __init__(self, store):
            self.store = store

        async def start_daily_decay(self):
            await waits_forever()

    monkeypatch.setattr(store_mod, "MemoryStore", FakeMemoryStore)
    monkeypatch.setattr(decay_mod, "MemoryDecay", FakeDecay)
    monkeypatch.setattr(scheduler, "stop_task_scheduler", AsyncMock())
    monkeypatch.setattr(kb_scheduler, "shutdown_kb_scheduler", AsyncMock())

    before = set(app_mod._APP_BG_TASKS)
    await app_mod._start_memory_decay_scheduler()
    spawned = [task for task in app_mod._APP_BG_TASKS if task not in before]
    assert len(spawned) == 1
    await asyncio.wait_for(started.wait(), timeout=1)

    await app_mod._stop_leader_schedulers(spawned)

    store = FakeMemoryStore.instances[0]
    assert spawned[0].cancelled()
    store.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_decay_app_shutdown_cancels_and_closes_once(monkeypatch):
    """应用 shutdown 通过 _APP_BG_TASKS 取消 owner，须先关 decay 连接再关共享池。"""
    import importlib

    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.graph as graph
    import swarm.brain.scheduler as scheduler
    import swarm.infra.db as db
    import swarm.infra.scheduler_leadership as leadership
    import swarm.knowledge.scheduler as kb_scheduler
    import swarm.memory.decay as decay_mod
    import swarm.memory.store as store_mod
    import swarm.tracing as tracing
    import swarm.worker.sandbox_pool as sandbox_pool

    started = asyncio.Event()
    FakeMemoryStore.instances.clear()
    FakeMemoryStore.connect_error = None

    class FakeDecay:
        def __init__(self, store):
            self.store = store

        async def start_daily_decay(self):
            started.set()
            await asyncio.Future()

    monkeypatch.setattr(store_mod, "MemoryStore", FakeMemoryStore)
    monkeypatch.setattr(decay_mod, "MemoryDecay", FakeDecay)
    monkeypatch.setattr(kb_scheduler, "shutdown_kb_scheduler", AsyncMock())
    monkeypatch.setattr(scheduler, "stop_task_scheduler", AsyncMock())
    monkeypatch.setattr(db, "close_async_pools", AsyncMock())
    monkeypatch.setattr(db, "close_sync_pools", lambda: None)
    monkeypatch.setattr(graph, "close_postgres_checkpointer", AsyncMock())
    monkeypatch.setattr(leadership, "close_coordination_backend", AsyncMock())
    monkeypatch.setattr(sandbox_pool, "pool_enabled", lambda: False)
    monkeypatch.setattr(tracing, "shutdown_tracing", lambda **_: None)

    original_tasks = set(app_mod._APP_BG_TASKS)
    app_mod._APP_BG_TASKS.clear()
    try:
        await app_mod._start_memory_decay_scheduler()
        await asyncio.wait_for(started.wait(), timeout=1)
        spawned = list(app_mod._APP_BG_TASKS)
        assert len(spawned) == 1

        await app_mod.on_shutdown()

        assert spawned[0].cancelled()
        FakeMemoryStore.instances[0].close.assert_awaited_once()
    finally:
        app_mod._APP_BG_TASKS.update(task for task in original_tasks if not task.done())


@pytest.mark.asyncio
async def test_memory_decay_connect_failure_still_closes_once(monkeypatch, caplog):
    from swarm.infra.degrade import degrade_counts

    before = degrade_counts().get("memory.decay.scheduler_error", 0)
    async def never_runs():
        raise AssertionError("decay must not run after connect failure")

    owned = await _capture_owned_decay_coro(
        monkeypatch, never_runs, connect_error=RuntimeError("connect exploded")
    )
    await owned

    store = FakeMemoryStore.instances[0]
    store.close.assert_awaited_once()
    assert "connect exploded" in caplog.text
    assert degrade_counts().get("memory.decay.scheduler_error", 0) == before + 1
