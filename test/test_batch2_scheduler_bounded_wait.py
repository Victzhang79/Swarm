"""Batch 2：协调后端与 scheduler 停机必须具有真实的有界等待。"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading

import pytest

import swarm.brain.scheduler as scheduler
import swarm.infra.scheduler_leadership as leadership


def test_fast_cleanup_failure_is_retrieved_and_logged(caplog):
    """超时失效清理快速失败也不能留下 Task exception was never retrieved。"""
    from swarm.infra.coordination import _invalidate_timed_out_backend

    class Backend:
        async def invalidate_after_timeout(self, _epoch):
            raise RuntimeError("cleanup failed")

    async def scenario():
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            await _invalidate_timed_out_backend(Backend(), 1.0, 1)
            gc.collect()
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            loop.set_exception_handler(previous)

    with caplog.at_level(logging.WARNING, logger="swarm.infra.coordination"):
        asyncio.run(scenario())
    assert "超时连接清理失败" in caplog.text


class _NeverReturningBackend:
    def __init__(self) -> None:
        self.closed = 0

    async def verify_leadership(self, _key):
        await asyncio.Event().wait()

    async def is_held(self, _key):
        await asyncio.Event().wait()

    async def close(self):
        self.closed += 1


async def _finish_or_cancel(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_coordination_init_failure_is_not_single_instance_fallback(monkeypatch):
    """协调探活失败不能用 None 冒充显式单机，否则每个副本都会自认 leader。"""
    class Backend:
        async def close(self):
            return None

    async def fail_probe(*_args, **_kwargs):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(leadership, "_backend", None)
    monkeypatch.setattr(
        "swarm.infra.coordination.PgCoordinationBackend", lambda _uri: Backend()
    )
    monkeypatch.setattr(leadership, "run_coordination_operation", fail_probe)

    with pytest.raises(RuntimeError, match="probe failed"):
        asyncio.run(leadership.init_coordination_backend("postgresql://unused"))
    assert leadership.get_coordination_backend() is None


def test_real_pg_probe_connection_failure_propagates_from_init(monkeypatch):
    """生产 Pg backend 的连接异常不能被 acquire 的 False 折叠成“startup 成功”。"""
    from swarm.infra.coordination import PgCoordinationBackend

    async def fail_connect(self):
        raise OSError("db down")

    monkeypatch.setattr(leadership, "_backend", None)
    monkeypatch.setattr(PgCoordinationBackend, "_ensure_conn", fail_connect)
    with pytest.raises(OSError, match="db down"):
        asyncio.run(leadership.init_coordination_backend("postgresql://unused"))
    assert leadership.get_coordination_backend() is None


def test_coordination_close_is_bounded_and_detaches_backend(monkeypatch):
    """半开 driver 的 close 不能永久卡住 API shutdown。"""
    class HangingBackend:
        async def close(self):
            await asyncio.Event().wait()

    backend = HangingBackend()
    monkeypatch.setenv("SWARM_COORDINATION_OPERATION_TIMEOUT_SEC", "0.03")
    monkeypatch.setattr(leadership, "_backend", backend)

    async def scenario():
        closing = asyncio.create_task(leadership.close_coordination_backend())
        done, _pending = await asyncio.wait({closing}, timeout=0.2)
        assert closing in done, "coordination close 必须有界返回"
        await closing
        assert leadership.get_coordination_backend() is None

    asyncio.run(scenario())


def test_execution_readiness_times_out_half_open_coordination_fail_closed(monkeypatch):
    """readiness 不能被半开 verify_leadership 永久占住，且须清掉失主连接。"""
    import importlib

    app_module = importlib.import_module("swarm.api.app")

    backend = _NeverReturningBackend()
    monkeypatch.setenv("SWARM_COORDINATION_OPERATION_TIMEOUT_SEC", "0.03")
    monkeypatch.setattr(leadership, "_backend", backend)
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)

    async def scenario():
        probe = asyncio.create_task(app_module._probe_execution_plane_ready())
        done, _pending = await asyncio.wait({probe}, timeout=0.2)
        try:
            assert probe in done, "readiness 不得被半开协调连接永久挂起"
            assert await probe == (False, "leader_status_unavailable")
            assert backend.closed == 1
        finally:
            await _finish_or_cancel(probe)

    asyncio.run(scenario())


def test_submit_times_out_half_open_coordination_without_enqueue(monkeypatch):
    """submit 验主超时必须拒绝且不得在不确定 leadership 下入队。"""
    backend = _NeverReturningBackend()
    enqueued: list[str] = []
    monkeypatch.setenv("SWARM_COORDINATION_OPERATION_TIMEOUT_SEC", "0.03")
    monkeypatch.setattr(leadership, "_backend", backend)
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    monkeypatch.setattr(scheduler.TaskQueue, "enqueue", lambda task_id, *_a, **_k: enqueued.append(task_id))
    scheduler._stopping = False

    async def scenario():
        submit = asyncio.create_task(scheduler.submit_task("half-open", "p", "desc"))
        done, _pending = await asyncio.wait({submit}, timeout=0.2)
        try:
            assert submit in done, "submit 不得被半开协调连接永久挂起"
            assert await submit is scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST
            assert enqueued == []
            assert backend.closed == 1
        finally:
            await _finish_or_cancel(submit)

    asyncio.run(scenario())


def test_pg_coordination_timeout_clears_held_state_and_detaches_connection(monkeypatch):
    """生产 PG backend 半开时先失主、摘连接，再向调用方报告超时。"""
    from swarm.infra.coordination import (
        CoordinationOperationTimeout,
        PgCoordinationBackend,
        run_coordination_operation,
    )

    class HangingCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args):
            await asyncio.Event().wait()

        async def fetchone(self):
            return (1,)

    class HalfOpenConn:
        closed = False

        def __init__(self):
            self.close_calls = 0

        def cursor(self):
            return HangingCursor()

        async def close(self):
            self.close_calls += 1
            self.closed = True

    monkeypatch.setenv("SWARM_COORDINATION_OPERATION_TIMEOUT_SEC", "0.03")
    backend = PgCoordinationBackend("postgresql://unused")
    conn = HalfOpenConn()
    backend._conn = conn
    backend._held.add("scheduler:all")

    async def scenario():
        with pytest.raises(CoordinationOperationTimeout):
            await run_coordination_operation(
                backend, "verify_leadership", "scheduler:all")
        assert backend._held == set()
        assert backend._conn is None
        assert conn.close_calls == 1

    asyncio.run(scenario())


def test_caller_cancel_does_not_invalidate_shared_coordination_backend(monkeypatch):
    """HTTP/上游取消不是失主证据，不能顺带关闭进程共享的健康 backend。"""
    from swarm.infra.coordination import run_coordination_operation

    backend = _NeverReturningBackend()
    monkeypatch.setenv("SWARM_COORDINATION_OPERATION_TIMEOUT_SEC", "5")

    async def scenario():
        operation = asyncio.create_task(
            run_coordination_operation(
                backend, "verify_leadership", "scheduler:all",
            )
        )
        await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await asyncio.sleep(0)
        assert backend.closed == 0

    asyncio.run(scenario())


def test_stale_coordination_timeout_cannot_clear_reconnected_epoch(monkeypatch):
    """旧探活 deadline 晚到时只能关旧连接，不得清掉新 epoch 的锁和连接。"""
    from swarm.infra.coordination import (
        CoordinationOperationTimeout,
        PgCoordinationBackend,
        run_coordination_operation,
    )

    started = asyncio.Event()

    class HangingCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args):
            started.set()
            await asyncio.Event().wait()

        async def fetchone(self):
            return (1,)

    class Connection:
        closed = False

        def __init__(self, *, hanging: bool):
            self.hanging = hanging
            self.close_calls = 0

        def cursor(self):
            assert self.hanging
            return HangingCursor()

        async def close(self):
            self.close_calls += 1
            self.closed = True

    monkeypatch.setenv("SWARM_COORDINATION_OPERATION_TIMEOUT_SEC", "0.03")
    backend = PgCoordinationBackend("postgresql://unused")
    old_conn = Connection(hanging=True)
    new_conn = Connection(hanging=False)
    backend._conn = old_conn
    backend._held.add("scheduler:all")

    async def scenario():
        operation = asyncio.create_task(
            run_coordination_operation(
                backend, "verify_leadership", "scheduler:all",
            )
        )
        await started.wait()
        backend._conn = new_conn
        backend._connection_generation += 1
        backend._held = {"scheduler:all"}

        with pytest.raises(CoordinationOperationTimeout):
            await operation

        assert backend._conn is new_conn
        assert backend._held == {"scheduler:all"}
        assert new_conn.close_calls == 0
        assert old_conn.close_calls == 1

    asyncio.run(scenario())


def test_concurrent_coordination_acquire_singleflights_connection(monkeypatch):
    """并发首次抢锁只能创建一条 PG session，所有 key 必须归属同一真实会话。"""
    import psycopg

    from swarm.infra.coordination import PgCoordinationBackend

    connections = []

    class Cursor:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _query, params):
            self.conn.lock_ids.append(params[0])

        async def fetchone(self):
            return (True,)

    class Connection:
        closed = False

        def __init__(self):
            self.lock_ids = []

        def cursor(self):
            return Cursor(self)

    async def connect(*_args, **_kwargs):
        conn = Connection()
        connections.append(conn)
        # 主动让出事件循环，稳定暴露无 singleflight 时的双连接竞态。
        await asyncio.sleep(0.02)
        return conn

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    backend = PgCoordinationBackend("postgresql://unused")

    async def scenario():
        acquired = await asyncio.gather(
            backend.try_acquire_leadership("scheduler:a"),
            backend.try_acquire_leadership("scheduler:b"),
        )
        assert acquired == [True, True]
        assert len(connections) == 1
        assert len(connections[0].lock_ids) == 2
        assert backend._conn is connections[0]
        assert backend._held == {"scheduler:a", "scheduler:b"}

    asyncio.run(scenario())


def test_coordination_query_error_closes_failed_session_and_loses_ownership():
    """真实查询错误必须关失败 session；不能只清 Python 引用而泄漏 advisory lock。"""
    from swarm.infra.coordination import PgCoordinationBackend

    class FailingCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args):
            raise ConnectionError("connection lost")

    class Connection:
        closed = False

        def __init__(self):
            self.close_calls = 0

        def cursor(self):
            return FailingCursor()

        async def close(self):
            self.close_calls += 1
            self.closed = True

    backend = PgCoordinationBackend("postgresql://unused")
    conn = Connection()
    backend._conn = conn
    backend._held = {"scheduler:existing"}

    async def scenario():
        assert await backend.try_acquire_leadership("scheduler:new") is False
        assert conn.close_calls == 1
        assert backend._conn is None
        assert backend._held == set()

    asyncio.run(scenario())


def test_none_connection_epoch_invalidates_connection_created_by_same_operation():
    """操作开始尚无连接时，deadline 仍须撤销该操作随后发布的同 epoch session。"""
    from swarm.infra.coordination import PgCoordinationBackend

    class Connection:
        closed = False

        def __init__(self):
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            self.closed = True

    backend = PgCoordinationBackend("postgresql://unused")
    operation_epoch = backend.coordination_operation_epoch()
    conn = Connection()
    backend._conn = conn
    backend._held = {"scheduler:all"}

    async def scenario():
        await backend.invalidate_after_timeout(operation_epoch)
        assert conn.close_calls == 1
        assert backend._conn is None
        assert backend._held == set()

    asyncio.run(scenario())


def test_await_slot_rechecks_consumer_after_leadership_await(monkeypatch):
    """验主让出事件循环期间 consumer 死亡，最终 add 前必须 fail-closed。"""
    alive = {"value": True}

    async def verify_then_die():
        await asyncio.sleep(0)
        alive["value"] = False
        return True

    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: alive["value"])
    monkeypatch.setattr(scheduler, "_max_concurrent", lambda: 1)
    monkeypatch.setattr(scheduler, "verify_local_execution_leadership", verify_then_die)
    scheduler._stopping = False
    scheduler._inflight.discard("consumer-died-at-verify")

    async def scenario():
        result = await scheduler.await_execution_slot("consumer-died-at-verify")
        assert result is scheduler.ExecutionAdmission.REJECTED_UNAVAILABLE
        assert "consumer-died-at-verify" not in scheduler._inflight

    asyncio.run(scenario())


def test_await_slot_stops_waiting_when_consumer_dies(monkeypatch):
    """满槽等待期间 consumer 死亡时应早退，不能继续等到总 slot timeout。"""
    alive = {"value": True}
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: alive["value"])
    monkeypatch.setattr(scheduler, "_max_concurrent", lambda: 1)
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", lambda: asyncio.sleep(0, result=True),
    )
    monkeypatch.setenv("SWARM_RESUME_SLOT_WAIT_S", "5")
    scheduler._stopping = False
    scheduler._inflight.add("existing-slot")

    async def scenario():
        waiter = asyncio.create_task(scheduler.await_execution_slot("waiting-slot"))
        await asyncio.sleep(0.05)
        alive["value"] = False
        done, _pending = await asyncio.wait({waiter}, timeout=0.5)
        try:
            assert waiter in done
            assert await waiter is scheduler.ExecutionAdmission.REJECTED_UNAVAILABLE
            assert "waiting-slot" not in scheduler._inflight
        finally:
            await _finish_or_cancel(waiter)

    try:
        asyncio.run(scenario())
    finally:
        scheduler._inflight.discard("existing-slot")
        scheduler._inflight.discard("waiting-slot")


def test_consumer_requeues_dequeued_item_when_resume_takes_last_slot(monkeypatch):
    """consumer 的异步准入窗口不能让 resume 抢槽后仍超额派发。"""
    from swarm.infra import redis_client

    admission_started = threading.Event()
    admission_release = threading.Event()
    dispatched: list[str] = []

    def delayed_project_admission(_project_id: str) -> str:
        admission_started.set()
        assert admission_release.wait(timeout=2)
        return "ready"

    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    monkeypatch.setattr(scheduler, "_max_concurrent", lambda: 1)
    monkeypatch.setattr(scheduler, "_project_exec_admission", delayed_project_admission)
    monkeypatch.setattr(
        scheduler,
        "verify_local_execution_leadership",
        lambda: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        scheduler,
        "_resolve_exec_meta",
        lambda _task_id: {
            "project_id": "project-race",
            "description": "queued work",
            "auto_accept": False,
        },
    )
    monkeypatch.setattr(
        scheduler,
        "_run_with_slot",
        lambda task_id, *_args: dispatched.append(task_id),
    )
    monkeypatch.setattr(
        scheduler,
        "_maybe_drain_stranded",
        lambda **_kwargs: asyncio.sleep(0),
    )

    async def scenario() -> None:
        await scheduler.stop_task_scheduler()
        scheduler.TaskQueue._clear_memory()
        scheduler._pending_meta.clear()
        scheduler._inflight.clear()
        scheduler.TaskQueue.enqueue(
            "queued-during-resume", "project-race", priority="urgent"
        )
        scheduler._pending_meta["queued-during-resume"] = {
            "project_id": "project-race",
            "description": "queued work",
            "auto_accept": False,
        }
        await scheduler.start_task_scheduler()
        try:
            assert await asyncio.to_thread(admission_started.wait, 1)
            admission = await scheduler.await_execution_slot("resume-takes-slot")
            assert admission is scheduler.ExecutionAdmission.SLOTTED
            admission_release.set()

            for _ in range(100):
                if scheduler.TaskQueue.queued_task_ids() == {"queued-during-resume"}:
                    break
                await asyncio.sleep(0.01)

            assert dispatched == []
            assert scheduler._inflight == {"resume-takes-slot"}
            queued = scheduler.TaskQueue.dequeue()
            assert queued == {
                "task_id": "queued-during-resume",
                "project_id": "project-race",
                "priority": "urgent",
            }
        finally:
            admission_release.set()
            await scheduler.stop_task_scheduler()
            scheduler.TaskQueue._clear_memory()
            scheduler._pending_meta.clear()
            scheduler._inflight.clear()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_point", ["admission", "leadership"])
def test_consumer_stop_requeues_owned_dequeued_item(monkeypatch, cancel_point):
    """stop 取消落在出队后的 await 时，尚未派发的原优先级项必须立即归队。"""
    from swarm.infra import redis_client

    admission_started = threading.Event()
    admission_release = threading.Event()
    leadership_started = asyncio.Event()
    leadership_release = asyncio.Event()
    dispatched: list[str] = []

    def project_admission(_project_id: str) -> str:
        if cancel_point == "admission":
            admission_started.set()
            assert admission_release.wait(timeout=2)
        return "ready"

    async def verify_leadership() -> bool:
        if cancel_point == "leadership":
            leadership_started.set()
            await leadership_release.wait()
        return True

    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    monkeypatch.setattr(scheduler, "_max_concurrent", lambda: 1)
    monkeypatch.setattr(scheduler, "_project_exec_admission", project_admission)
    monkeypatch.setattr(scheduler, "verify_local_execution_leadership", verify_leadership)
    monkeypatch.setattr(
        scheduler,
        "_resolve_exec_meta",
        lambda _task_id: {
            "project_id": "project-stop",
            "description": "queued work",
            "auto_accept": False,
        },
    )
    monkeypatch.setattr(
        scheduler,
        "_run_with_slot",
        lambda task_id, *_args: dispatched.append(task_id),
    )
    monkeypatch.setattr(
        scheduler,
        "_maybe_drain_stranded",
        lambda **_kwargs: asyncio.sleep(0),
    )

    async def scenario() -> None:
        await scheduler.stop_task_scheduler()
        scheduler.TaskQueue._clear_memory()
        scheduler._pending_meta.clear()
        scheduler._inflight.clear()
        scheduler.TaskQueue.enqueue(
            "owned-at-stop", "project-stop", priority="urgent"
        )
        scheduler._pending_meta["owned-at-stop"] = {
            "project_id": "project-stop",
            "description": "queued work",
            "auto_accept": False,
        }
        await scheduler.start_task_scheduler()
        try:
            if cancel_point == "admission":
                assert await asyncio.to_thread(admission_started.wait, 1)
            else:
                await asyncio.wait_for(leadership_started.wait(), timeout=1)

            await scheduler.stop_task_scheduler()

            assert dispatched == []
            queued = scheduler.TaskQueue.dequeue()
            assert queued == {
                "task_id": "owned-at-stop",
                "project_id": "project-stop",
                "priority": "urgent",
            }
        finally:
            admission_release.set()
            leadership_release.set()
            await scheduler.stop_task_scheduler()
            scheduler.TaskQueue._clear_memory()
            scheduler._pending_meta.clear()
            scheduler._inflight.clear()

    asyncio.run(scenario())


def test_consumer_cancel_requeue_failure_is_machine_readable_and_preserves_cancel(
    monkeypatch, caplog,
):
    """补偿 enqueue 失败要留机读告警，但不能把 consumer 的原始取消吞掉。"""
    from swarm.infra import redis_client

    leadership_started = asyncio.Event()
    leadership_release = asyncio.Event()

    async def blocked_leadership() -> bool:
        leadership_started.set()
        await leadership_release.wait()
        return True

    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    monkeypatch.setattr(scheduler, "_max_concurrent", lambda: 1)
    monkeypatch.setattr(scheduler, "_project_exec_admission", lambda _pid: "ready")
    monkeypatch.setattr(scheduler, "verify_local_execution_leadership", blocked_leadership)
    monkeypatch.setattr(
        scheduler,
        "_resolve_exec_meta",
        lambda _task_id: {
            "project_id": "project-stop",
            "description": "queued work",
            "auto_accept": False,
        },
    )
    monkeypatch.setattr(
        scheduler,
        "_maybe_drain_stranded",
        lambda **_kwargs: asyncio.sleep(0),
    )

    async def scenario() -> None:
        await scheduler.stop_task_scheduler()
        scheduler.TaskQueue._clear_memory()
        scheduler._pending_meta.clear()
        scheduler._inflight.clear()
        scheduler.TaskQueue.enqueue(
            "requeue-will-fail", "project-stop", priority="background"
        )
        scheduler._pending_meta["requeue-will-fail"] = {
            "project_id": "project-stop",
            "description": "queued work",
            "auto_accept": False,
        }
        monkeypatch.setattr(
            scheduler.TaskQueue,
            "enqueue",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("redis down")),
        )
        await scheduler.start_task_scheduler()
        consumer = scheduler._consumer_task
        assert consumer is not None
        try:
            await asyncio.wait_for(leadership_started.wait(), timeout=1)
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
            assert "requeue-will-fail" in scheduler._pending_meta
        finally:
            leadership_release.set()
            await scheduler.stop_task_scheduler()
            scheduler.TaskQueue._clear_memory()
            scheduler._pending_meta.clear()
            scheduler._inflight.clear()

    with caplog.at_level(logging.WARNING, logger="swarm.brain.scheduler"):
        asyncio.run(scenario())
    assert "degraded=scheduler_dequeued_requeue_failed" in caplog.text
    assert "task=requeue-will-fail" in caplog.text
