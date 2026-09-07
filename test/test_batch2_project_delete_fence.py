"""Batch2：项目删除围栏与写盘 owner 的取消屏障。"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_preprocess_lock_recheck_rejects_deleting_before_any_phase(monkeypatch):
    import swarm.project.preprocess as preprocess
    import swarm.project.store as store

    lock = SimpleNamespace(acquire=lambda: True, release_calls=0)

    def release():
        lock.release_calls += 1

    lock.release = release
    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", lambda *_a: lock)
    monkeypatch.setattr(store, "get_project", lambda _pid: {"id": _pid, "status": "DELETING"})
    phases = AsyncMock()
    monkeypatch.setattr(preprocess, "_preprocess_project_under_lock", phases)

    with pytest.raises(store.ProjectDeletionInProgressError):
        await preprocess.preprocess_project("p", "/tmp")
    phases.assert_not_awaited()
    assert lock.release_calls == 1


@pytest.mark.asyncio
async def test_preprocess_renews_long_lived_lock_and_stops_before_release_on_loss(monkeypatch):
    import swarm.project.preprocess as preprocess
    import swarm.project.store as store

    events: list[str] = []

    class Lock:
        ttl_sec = 0.05

        def acquire(self):
            return True

        def renew(self):
            events.append("renew-failed")
            return False

        def release(self):
            events.append("lock-released")

    async def phases(*_args):
        ev = preprocess._register_cancel_event("p")
        try:
            while not ev.is_set():
                await asyncio.sleep(0.005)
        finally:
            preprocess._unregister_cancel_event("p", ev)
            events.append("phases-stopped")

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", lambda *_a: Lock())
    monkeypatch.setattr(store, "get_project", lambda _pid: {"id": _pid, "status": "READY"})
    monkeypatch.setattr(preprocess, "_preprocess_project_under_lock", phases)

    with pytest.raises(RuntimeError, match="lost ModuleLock"):
        await preprocess.preprocess_project("p", "/tmp")
    assert events == ["renew-failed", "phases-stopped", "lock-released"]


@pytest.mark.asyncio
async def test_preprocess_lock_loss_drains_writer_and_blocks_next_write_boundary(monkeypatch):
    """非合作同步 writer 返回前不得释放锁；返回后失锁边界必须截断下一次写。"""
    import swarm.project.preprocess as preprocess
    import swarm.project.store as store

    writer_started = threading.Event()
    writer_finish = threading.Event()
    events: list[str] = []
    monkeypatch.setenv("SWARM_LOCK_RENEW_INTERVAL_SEC", "0.01")

    class Lock:
        ttl_sec = 0.05

        def acquire(self):
            return True

        def renew(self):
            events.append("renew-failed")
            return False

        def release(self):
            events.append("lock-released")

    def first_writer():
        writer_started.set()
        writer_finish.wait(timeout=2)
        events.append("first-writer-returned")

    def forbidden_second_writer():
        events.append("second-writer-entered")

    async def phases(*_args):
        await preprocess._preprocess_blocking(first_writer)
        await preprocess._preprocess_blocking(forbidden_second_writer)

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", lambda *_a: Lock())
    monkeypatch.setattr(store, "get_project", lambda _pid: {"id": _pid, "status": "READY"})
    monkeypatch.setattr(preprocess, "_preprocess_project_under_lock", phases)

    owned = asyncio.create_task(preprocess.preprocess_project("p", "/tmp"))
    assert await asyncio.to_thread(writer_started.wait, 1)
    await asyncio.sleep(0.08)
    assert "renew-failed" in events
    assert "lock-released" not in events
    writer_finish.set()

    with pytest.raises(preprocess.PreprocessOwnershipLostError):
        await owned
    assert "second-writer-entered" not in events
    assert events[-2:] == ["first-writer-returned", "lock-released"]


@pytest.mark.asyncio
async def test_preprocess_lock_loss_cancels_async_phase_before_second_write(monkeypatch):
    """不查询 threading.Event 的异步 phase 也必须在续租失锁时被主动截断。"""
    import swarm.project.preprocess as preprocess
    import swarm.project.store as store

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    renew_failed = asyncio.Event()
    events: list[str] = []
    monkeypatch.setenv("SWARM_LOCK_RENEW_INTERVAL_SEC", "0.01")

    class Lock:
        ttl_sec = 1

        def acquire(self):
            return True

        def renew(self):
            renew_failed.set()
            return False

        def release(self):
            events.append("lock-released")

    async def phases(*_args):
        events.append("first-async-write")
        first_started.set()
        await release_first.wait()
        events.append("second-async-write")

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", lambda *_a: Lock())
    monkeypatch.setattr(store, "get_project", lambda _pid: {"id": _pid, "status": "READY"})
    monkeypatch.setattr(preprocess, "_preprocess_project_under_lock", phases)

    owned = asyncio.create_task(preprocess.preprocess_project("p", "/tmp"))
    await first_started.wait()
    await asyncio.wait_for(renew_failed.wait(), timeout=1)
    await asyncio.sleep(0.03)
    release_first.set()
    with pytest.raises(preprocess.PreprocessOwnershipLostError):
        await owned

    assert "second-async-write" not in events
    assert events[-1] == "lock-released"


@pytest.mark.asyncio
async def test_preprocess_renew_exception_cancels_phase_and_becomes_ownership_loss(monkeypatch):
    """renew 抛错与返回 False 同义：当前 owner 不能继续下一次异步写。"""
    import swarm.project.preprocess as preprocess
    import swarm.project.store as store

    renew_called = asyncio.Event()
    release_first = asyncio.Event()
    events: list[str] = []
    monkeypatch.setenv("SWARM_LOCK_RENEW_INTERVAL_SEC", "0.01")

    class Lock:
        ttl_sec = 1

        def acquire(self):
            return True

        def renew(self):
            renew_called.set()
            raise OSError("redis broken")

        def release(self):
            events.append("lock-released")

    async def phases(*_args):
        events.append("first-async-write")
        await release_first.wait()
        events.append("second-async-write")

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", lambda *_a: Lock())
    monkeypatch.setattr(store, "get_project", lambda _pid: {"id": _pid, "status": "READY"})
    monkeypatch.setattr(preprocess, "_preprocess_project_under_lock", phases)

    owned = asyncio.create_task(preprocess.preprocess_project("p", "/tmp"))
    await asyncio.wait_for(renew_called.wait(), timeout=1)
    await asyncio.sleep(0.03)
    release_first.set()
    with pytest.raises(preprocess.PreprocessOwnershipLostError):
        await owned

    assert "second-async-write" not in events
    assert events[-1] == "lock-released"


@pytest.mark.asyncio
async def test_shutdown_racing_confirmed_renew_loss_does_not_settle_new_owner(monkeypatch):
    """renew 已在同步边界确认失锁时，迟到 shutdown 取消不得以旧 owner 身份写 ERROR。"""
    import swarm.project.preprocess as preprocess
    import swarm.project.store as store

    loop = asyncio.get_running_loop()
    renew_entered = threading.Event()
    allow_renew_result = threading.Event()
    phase_started = asyncio.Event()
    settled: list[str] = []
    owner: asyncio.Task | None = None
    monkeypatch.setenv("SWARM_LOCK_RENEW_INTERVAL_SEC", "0.01")

    class Lock:
        ttl_sec = 1

        def acquire(self):
            return True

        def renew(self):
            renew_entered.set()
            allow_renew_result.wait(timeout=2)
            # 模拟 Redis 已回答 token 不再属于旧 owner；先投递 shutdown cancel，
            # 再让 asyncio.to_thread 的完成回调入队，制造 coroutine 未消费 False 的窗口。
            assert owner is not None
            loop.call_soon_threadsafe(owner.cancel)
            return False

        def release(self):
            return None

    async def phases(*_args):
        phase_started.set()
        await asyncio.Future()

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", lambda *_a: Lock())
    monkeypatch.setattr(store, "get_project", lambda _pid: {"id": _pid, "status": "READY"})
    monkeypatch.setattr(store, "settle_cancelled_preprocess", lambda pid: settled.append(pid))
    monkeypatch.setattr(preprocess, "_preprocess_project_under_lock", phases)

    owner = asyncio.create_task(preprocess.preprocess_project("p", "/tmp"))
    await phase_started.wait()
    assert await asyncio.to_thread(renew_entered.wait, 1)
    allow_renew_result.set()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert settled == [], "确认失锁后旧 owner 不得覆盖跨进程新 owner 的 PREPROCESSING"


@pytest.mark.asyncio
async def test_preprocess_cancel_drains_inflight_thread_before_releasing_lock(monkeypatch):
    import swarm.project.preprocess as preprocess
    import swarm.project.store as store

    started = threading.Event()
    finish = threading.Event()

    class Lock:
        ttl_sec = 3600
        released = 0

        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            type(self).released += 1

    def writer():
        started.set()
        finish.wait(timeout=2)

    async def phases(*_args):
        await preprocess._preprocess_blocking(writer)

    Lock.released = 0
    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", lambda *_a: Lock())
    monkeypatch.setattr(store, "get_project", lambda _pid: {"id": _pid, "status": "READY"})
    monkeypatch.setattr(store, "settle_cancelled_preprocess", lambda _pid: True)
    monkeypatch.setattr(preprocess, "_preprocess_project_under_lock", phases)

    owned = asyncio.create_task(preprocess.preprocess_project("p", "/tmp"))
    assert await asyncio.to_thread(started.wait, 1)
    owned.cancel()
    await asyncio.sleep(0.02)
    assert not owned.done()
    assert Lock.released == 0
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await owned
    assert Lock.released == 1


@pytest.mark.asyncio
async def test_preprocess_internal_timeout_drains_writer_before_releasing_lock(monkeypatch):
    """pipeline 自身 wait_for 超时也必须 join 同步 writer，不能只保护外层取消。"""
    import swarm.project.preprocess as preprocess
    import swarm.project.store as store

    started = threading.Event()
    finish = threading.Event()

    class Lock:
        ttl_sec = 3600
        released = 0

        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            type(self).released += 1

    def writer():
        started.set()
        finish.wait(timeout=2)

    async def blocking_scan(*_args):
        await preprocess._preprocess_blocking(writer)
        raise AssertionError("超时取消后不得越过 writer 边界")

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", lambda *_a: Lock())
    monkeypatch.setattr(store, "get_project", lambda _pid: {"id": _pid, "status": "READY"})
    monkeypatch.setattr(store, "upsert_progress", lambda *_a, **_kw: None)
    monkeypatch.setattr(store, "update_project", lambda *_a, **_kw: None)
    monkeypatch.setattr(preprocess, "_phase_scan", blocking_scan)
    monkeypatch.setattr(preprocess, "_preprocess_timeout_sec", lambda: 0.03)
    Lock.released = 0

    from swarm.infra.cancellation import OwnedBlockingCancelled

    owned = asyncio.create_task(preprocess.preprocess_project("p", "/tmp"))
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0.08)
    assert not owned.done()
    assert Lock.released == 0
    finish.set()
    try:
        await owned
    except OwnedBlockingCancelled as exc:
        # 解释器语义分叉（非时序）：py3.12 的 asyncio.Timeout.__aexit__ 只做
        # `exc_type is CancelledError` 身份判定（3.13+ 才改 issubclass），故 wait_for
        # 超时排空 writer 后 run_blocking_owned 抛出的 OwnedBlockingCancelled 子类
        # 在 3.12 上不被归一成 TimeoutError，而是经 preprocess_project 的取消路径
        # 结算后原样上抛。被测命题——「pipeline 自身 wait_for 超时也必须先 join 同步
        # writer 才放锁」——两版本一致（下方 released 断言不稀释）；此处只断言排空
        # 完成后 writer 的三态是 success（线程真结束，不是被遗弃）。
        assert exc.state == "success"
    assert Lock.released == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["task", "worker"])
async def test_manual_apply_cancel_waits_for_writer_before_lock_release(monkeypatch, endpoint):
    """请求取消不能让 apply 线程仍写盘时提前释放项目锁。"""
    from swarm.api._shared import ApplyDiffRequest
    import swarm.api.routers.task as task_router
    import swarm.api.routers.worker as worker_router

    started = threading.Event()
    finish = threading.Event()

    class Lock:
        released = 0

        def __init__(self, *_a):
            pass

        def acquire(self):
            return True

        def release(self):
            type(self).released += 1

    def apply(*_a, **_kw):
        started.set()
        finish.wait(timeout=2)
        return {"ok": True}

    project = {"id": "p", "path": "/tmp", "status": "READY"}
    task = {
        "id": "t", "project_id": "p", "status": "DONE", "merged_diff": "patch",
        "thread_id": "epoch", "updated_at": "v1",
    }
    fake_store = SimpleNamespace(
        get_project=lambda _pid: project,
        get_task=lambda _tid: task,
    )
    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", Lock)
    monkeypatch.setattr("swarm.project.diff_apply.apply_git_diff", apply)
    monkeypatch.setattr(task_router._app, "store", fake_store)
    monkeypatch.setattr(task_router, "_require_task_access", lambda *_a, **_kw: task)
    monkeypatch.setattr(worker_router, "_require_perm", lambda *_a, **_kw: None)
    Lock.released = 0

    if endpoint == "task":
        call = task_router.apply_task_diff(
            "t", object(), ApplyDiffRequest(diff="patch", check_only=False)
        )
    else:
        call = worker_router.apply_project_diff(
            "p", ApplyDiffRequest(diff="patch", check_only=False), object()
        )
    owned = asyncio.create_task(call)
    assert await asyncio.to_thread(started.wait, 1)
    owned.cancel()
    await asyncio.sleep(0.02)
    assert not owned.done()
    assert Lock.released == 0
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await owned
    assert Lock.released == 1


@pytest.mark.asyncio
async def test_project_delete_cancel_during_lock_acquire_reclaims_late_lock(monkeypatch):
    import swarm.api.routers.project as project_router

    started = threading.Event()
    finish = threading.Event()

    class Lock:
        released = 0

        def __init__(self, *_a):
            pass

        def acquire(self):
            started.set()
            finish.wait(timeout=2)
            return True

        def release(self):
            type(self).released += 1

    fake_store = MagicMock()
    fake_store.get_project.return_value = {"id": "p", "status": "READY"}
    fake_store.list_tasks.return_value = []
    fake_store.claim_project_deletion.return_value = {"id": "p", "status": "DELETING"}
    monkeypatch.setattr(project_router._app, "store", fake_store)
    monkeypatch.setattr(project_router, "_require_perm", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        project_router._app, "require_local_execution_leader", AsyncMock()
    )
    monkeypatch.setattr(
        "swarm.brain.runner.cancel_project_tasks", AsyncMock(return_value=0)
    )
    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", Lock)
    Lock.released = 0

    request = asyncio.create_task(project_router.delete_project("p", object()))
    assert await asyncio.to_thread(started.wait, 1)
    request.cancel()
    await asyncio.sleep(0.02)
    assert not request.done()
    assert Lock.released == 0
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert Lock.released == 1
    fake_store.delete_project.assert_not_called()
