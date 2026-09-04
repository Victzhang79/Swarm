"""Batch 1：取消必须等待仍持有写树能力的阻塞/子任务真正收尾。

测试只观察两个公开 seam：``WorkerExecutor.run`` 与
``run_standalone_worker``。二者一旦把控制权还给上层，调用方就会释放
ModuleLock 或把任务写成终态；因此它们不得留下仍可修改工作树的线程/child。
"""

from __future__ import annotations

import asyncio
import errno
import threading
import time
from contextlib import contextmanager

import pytest

import swarm.brain.runner as brain_runner
import swarm.brain.nodes.verify as verify_nodes
import swarm.infra.redis_client as redis_client
import swarm.worker.runner as worker_runner
from swarm.types import Confidence, FileScope, SubTask, SubTaskDifficulty, WorkerOutput
from swarm.infra.cancellation import cancel_and_wait
from swarm.worker.executor import WorkerExecutor
from swarm.worker.git_flock import ProjectGitLockError, _ProjectGitFlock
from swarm.worker.l1_verdict import L1Verdict


def _executor(tmp_path) -> WorkerExecutor:
    subtask = SubTask(
        id="cancel-owned-blocking",
        description="修改 result.py",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["result.py"]),
    )
    return WorkerExecutor(subtask=subtask, project_path=str(tmp_path))


def test_worker_run_cancel_waits_for_owned_blocking_mutation(monkeypatch, tmp_path):
    """取消 run 时，正在真实产出阻塞边界内运行的写者必须先退出。

    ``_parse_produce_result`` 会调用 ``_get_git_diff``，后者可能执行
    ``git add -N`` 修改共享 index，因此是实际的写树边界，不是测试专用 helper。
    """
    executor = _executor(tmp_path)
    entered = threading.Event()
    allow_finish = threading.Event()
    finished = threading.Event()
    resources_released = threading.Event()

    async def prepare():
        return None

    async def locate():
        return "located", None

    async def code(_located):
        return None

    async def verify():
        verdict = L1Verdict(passed=True, source="deterministic", reason="ok")
        return True, {}, verdict

    async def agent(*_args, **_kwargs):
        return "SUMMARY: done\nCONFIDENCE: high"

    def blocking_parse(*_args, **_kwargs):
        entered.set()
        allow_finish.wait(timeout=2)
        finished.set()
        return WorkerOutput(
            subtask_id=executor.subtask.id,
            diff="",
            summary="done",
            confidence=Confidence.HIGH,
            l1_passed=True,
            l1_details={},
            execution_log="",
            notes="",
        )

    monkeypatch.setattr(executor, "_phase_prepare", prepare)
    monkeypatch.setattr(executor, "_phase_locate", locate)
    monkeypatch.setattr(executor, "_phase_code", code)
    monkeypatch.setattr(executor, "_phase_verify_loop", verify)
    monkeypatch.setattr(executor, "_run_agent", agent)
    monkeypatch.setattr(executor, "_parse_produce_result", blocking_parse)
    monkeypatch.setattr(executor, "kill_sandbox", resources_released.set)

    async def scenario():
        run_task = asyncio.create_task(executor.run())
        assert await asyncio.to_thread(entered.wait, 1), "必须进入真实产出阻塞边界"
        run_task.cancel()
        try:
            await asyncio.sleep(0.05)
            assert not run_task.done(), "阻塞写者未结束前 run 不得完成取消并交还资源所有权"
            assert not finished.is_set(), "测试窗口内阻塞写者应仍在运行"
            assert not resources_released.is_set(), "写者未结束前不得执行资源释放/终态收尾"
        finally:
            allow_finish.set()
            await asyncio.gather(run_task, return_exceptions=True)
        assert finished.is_set()
        assert resources_released.is_set()
        assert run_task.cancelled()

    asyncio.run(scenario())


def test_standalone_cancel_joins_child_before_releasing_module_lock(monkeypatch, tmp_path):
    """外层 standalone 被取消时，必须先取消并等待 child，再释放 ModuleLock。"""
    child_started = asyncio.Event()
    allow_child_finish = asyncio.Event()
    child_finished = asyncio.Event()
    lock_released = asyncio.Event()

    class FakeLock:
        def __init__(self, *_args, **_kwargs):
            self.key = "fake"

        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            lock_released.set()

    class FakeExecutor:
        def __init__(self, **_kwargs):
            self.execution_log: list[str] = []
            self.phase = type("Phase", (), {"value": "coding"})()

        async def run(self):
            child_started.set()
            try:
                await allow_child_finish.wait()
            finally:
                # 模拟 child 在 CancelledError 后仍需完成的异步收尾。
                await allow_child_finish.wait()
                child_finished.set()
            return WorkerOutput(
                subtask_id="standalone-owned-child",
                diff="",
                summary="done",
                confidence=Confidence.MEDIUM,
                l1_passed=True,
                l1_details={},
                execution_log="",
                notes="",
            )

    monkeypatch.setattr(worker_runner.store, "get_project", lambda _pid: {
        "id": "project", "path": str(tmp_path),
    })
    monkeypatch.setattr("swarm.tools.paths.set_workspace_root", lambda *_a, **_k: None)
    monkeypatch.setattr("swarm.worker.executor.WorkerExecutor", FakeExecutor)
    monkeypatch.setattr(redis_client, "ModuleLock", FakeLock)
    monkeypatch.setattr(redis_client, "MultiModuleLock", FakeLock)
    monkeypatch.setattr(redis_client.RenewPacer, "due", lambda *_a, **_k: False)

    async def scenario():
        outer = asyncio.create_task(worker_runner.run_standalone_worker(
            "standalone-owned-child", "project", "desc"))
        await asyncio.wait_for(child_started.wait(), timeout=1)
        outer.cancel()
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            assert not outer.done(), "child 未收尾前 standalone 不得完成取消"
            assert not lock_released.is_set(), "child 仍持写树能力时 ModuleLock 不得释放"
            assert not child_finished.is_set()
        finally:
            allow_child_finish.set()
            with pytest.raises(asyncio.CancelledError):
                await outer
        assert child_finished.is_set()
        assert lock_released.is_set()

    try:
        asyncio.run(scenario())
    finally:
        worker_runner._worker_running.discard("standalone-owned-child")
        worker_runner._worker_queues.pop("standalone-owned-child", None)


def test_cancel_and_wait_propagates_cancel_received_while_draining():
    """调用方在 drain 期间被取消时，不能以 cancelling>0 的正常返回吞掉取消。"""
    child_started = asyncio.Event()
    child_cleanup_started = asyncio.Event()
    allow_child_finish = asyncio.Event()
    child_finished = asyncio.Event()
    owner_returned = asyncio.Event()

    async def child():
        child_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            child_cleanup_started.set()
            await allow_child_finish.wait()
            child_finished.set()

    async def owner():
        owned = asyncio.create_task(child())
        await child_started.wait()
        await cancel_and_wait(owned, operation="测试 child")
        owner_returned.set()

    async def scenario():
        outer = asyncio.create_task(owner())
        await asyncio.wait_for(child_cleanup_started.wait(), timeout=1)
        outer.cancel()
        await asyncio.sleep(0)
        assert not outer.done(), "owned child 未结束前不得传播取消"
        allow_child_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert child_finished.is_set()
        assert not owner_returned.is_set(), "drain 期间的取消不得退化成正常返回"

    asyncio.run(scenario())


def test_standalone_cancel_during_lost_lock_drain_finishes_all_cleanup_then_propagates(
    monkeypatch, tmp_path,
):
    """丢锁收尾期间再取消：child 全结束、ModuleLock 已释放，外层才传播取消。"""
    child_started = asyncio.Event()
    child_cleanup_started = asyncio.Event()
    allow_child_finish = asyncio.Event()
    child_finished = asyncio.Event()
    lock_released = asyncio.Event()

    class LostLock:
        def __init__(self, *_args, **_kwargs):
            self.key = "lost-lock"

        def acquire(self):
            return True

        def renew(self):
            return False

        def release(self):
            lock_released.set()

    class CleaningExecutor:
        def __init__(self, **_kwargs):
            self.execution_log: list[str] = []
            self.phase = type("Phase", (), {"value": "coding"})()

        async def run(self):
            child_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                child_cleanup_started.set()
                await allow_child_finish.wait()
                child_finished.set()

    monkeypatch.setattr(worker_runner.store, "get_project", lambda _pid: {
        "id": "project", "path": str(tmp_path),
    })
    monkeypatch.setattr("swarm.tools.paths.set_workspace_root", lambda *_a, **_k: None)
    monkeypatch.setattr("swarm.worker.executor.WorkerExecutor", CleaningExecutor)
    monkeypatch.setattr(redis_client, "ModuleLock", LostLock)
    monkeypatch.setattr(redis_client, "MultiModuleLock", LostLock)
    monkeypatch.setattr(redis_client.RenewPacer, "due", lambda *_a, **_k: True)

    async def scenario():
        outer = asyncio.create_task(worker_runner.run_standalone_worker(
            "standalone-cancel-during-drain", "project", "desc"))
        await asyncio.wait_for(child_started.wait(), timeout=1)
        await asyncio.wait_for(child_cleanup_started.wait(), timeout=1)
        outer.cancel()
        await asyncio.sleep(0)
        assert not outer.done()
        assert not lock_released.is_set()
        allow_child_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert child_finished.is_set()
        assert lock_released.is_set()
        assert [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ] == []

    try:
        asyncio.run(scenario())
    finally:
        worker_runner._worker_running.discard("standalone-cancel-during-drain")
        worker_runner._worker_queues.pop("standalone-cancel-during-drain", None)


def test_brain_watchdog_stop_propagates_cancel_only_after_watchdog_cleanup():
    """Brain watchdog 的 stop seam 同样保留 drain 期间到达的取消语义。"""
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def watchdog():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
            cleanup_finished.set()

    async def scenario():
        watchdog_task = asyncio.create_task(watchdog())
        brain_runner._watchdog_tasks["cancel-drain-watchdog"] = watchdog_task
        stopper = asyncio.create_task(brain_runner._stop_watchdog(
            "cancel-drain-watchdog"))
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        stopper.cancel()
        await asyncio.sleep(0)
        assert not stopper.done()
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert watchdog_task.done()
        assert cleanup_finished.is_set()
        assert "cancel-drain-watchdog" not in brain_runner._watchdog_tasks

    try:
        asyncio.run(scenario())
    finally:
        brain_runner._watchdog_tasks.pop("cancel-drain-watchdog", None)


def test_owned_db_write_has_server_deadline_and_thread_finishes_before_return(monkeypatch):
    """owned PG 写必须在连接上装载 statement/lock timeout，超时后线程已收尾。"""
    import swarm.infra.db as db
    import swarm.project.store as project_store
    from swarm.infra.cancellation import run_db_blocking_owned

    settings = {"statement": "7s", "lock": "3s"}
    executed: list[str] = []
    writer_finished = threading.Event()

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            executed.append(str(sql))
            if str(sql).startswith("SHOW "):
                self._shown = "statement" if "statement_timeout" in str(sql) else "lock"
                return
            if "set_config" in str(sql):
                key = "statement" if "statement_timeout" in str(sql) else "lock"
                settings[key] = str((params or ("0",))[0])
                return
            if sql == "BLOCKED UPDATE":
                raw = settings["statement"]
                timeout_ms = int(raw.removesuffix("ms")) if raw.endswith("ms") else 0
                assert timeout_ms > 0
                time.sleep(timeout_ms / 1000)
                raise TimeoutError("server statement timeout")

        def fetchone(self):
            return (settings[self._shown],)

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    class FakePool:
        @contextmanager
        def connection(self):
            yield FakeConn()

    monkeypatch.setattr(db, "sync_pool", lambda *_a, **_k: FakePool())

    def blocked_write():
        try:
            with project_store._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("BLOCKED UPDATE")
        finally:
            writer_finished.set()

    async def scenario():
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="statement timeout"):
            await run_db_blocking_owned(
                blocked_write,
                operation="测试有界 PG 写",
                db_timeout_s=0.04,
            )
        assert time.monotonic() - started < 0.3
        assert writer_finished.is_set(), "helper 抛出前 DB writer 线程必须已经结束"
        assert settings == {"statement": "7s", "lock": "3s"}
        assert "SHOW statement_timeout" in executed
        assert "SHOW lock_timeout" in executed

    asyncio.run(scenario())


def test_runtime_smoke_cancel_reclaims_late_acquired_sandbox(monkeypatch):
    """acquire 取消后迟到返回的 sandbox 未赋给 caller，也必须由 owned helper 销毁。"""
    acquire_started = threading.Event()
    allow_acquire = threading.Event()
    acquire_finished = threading.Event()
    killed = threading.Event()
    killed_ids: list[str] = []
    sandbox = type("Sandbox", (), {"sandbox_id": "late-smoke-box"})()

    def late_acquire(*_args, **_kwargs):
        acquire_started.set()
        allow_acquire.wait(timeout=1)
        acquire_finished.set()
        return sandbox, None, {"source": "self_built"}

    def kill(sandbox_id):
        killed_ids.append(sandbox_id)
        killed.set()

    monkeypatch.setattr(verify_nodes, "_acquire_smoke_sandbox", late_acquire)
    monkeypatch.setattr(verify_nodes, "_kill_sandbox_quiet", kill)

    async def scenario():
        outer = asyncio.create_task(verify_nodes._acquire_smoke_sandbox_owned(
            object(), "", "project", "/project", 60, ""))
        assert await asyncio.to_thread(acquire_started.wait, 1)
        outer.cancel()
        await asyncio.sleep(0)
        assert not outer.done()
        assert not killed.is_set()
        allow_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert acquire_finished.is_set()
        assert killed_ids == ["late-smoke-box"]

    asyncio.run(scenario())


def test_project_git_flock_contention_has_monotonic_deadline(monkeypatch, tmp_path):
    """真实文件锁争用必须超时 fail-loud，不得永久占住 owned blocking 线程。"""
    monkeypatch.setenv("SWARM_GIT_FLOCK_ACQUIRE_TIMEOUT_SEC", "0.12")
    holder_entered = threading.Event()
    release_holder = threading.Event()
    waiter_done = threading.Event()
    errors: list[BaseException] = []

    def holder():
        with _ProjectGitFlock(tmp_path):
            holder_entered.set()
            release_holder.wait(timeout=2)

    def waiter():
        try:
            with _ProjectGitFlock(tmp_path):
                pass
        except BaseException as exc:  # noqa: BLE001 — 断言生产异常类型
            errors.append(exc)
        finally:
            waiter_done.set()

    hold_thread = threading.Thread(target=holder)
    wait_thread = threading.Thread(target=waiter)
    hold_thread.start()
    assert holder_entered.wait(timeout=1)
    started = time.monotonic()
    wait_thread.start()
    try:
        assert waiter_done.wait(timeout=0.6), "真实 flock 争用必须在配置 deadline 后 fail-loud"
        assert time.monotonic() - started < 0.6
        assert len(errors) == 1
        assert type(errors[0]).__name__ == "ProjectGitLockTimeout"
    finally:
        release_holder.set()
        hold_thread.join(timeout=1)
        wait_thread.join(timeout=1)


def test_project_git_flock_runtime_io_error_fails_loud():
    """ENOLCK/EIO 等非争用故障不能三次后无锁写共享工作树。"""
    class FakeFile:
        closed = False

        def close(self):
            self.closed = True

    class BrokenFcntl:
        LOCK_EX = 2
        LOCK_NB = 4

        def __init__(self):
            self.calls = 0

        def flock(self, *_args):
            self.calls += 1
            raise OSError(errno.EIO, "I/O error")

    lock = _ProjectGitFlock.__new__(_ProjectGitFlock)
    lock._lock_f = FakeFile()
    lock._fcntl = BrokenFcntl()
    lock._lock_path = None
    lock._acquire_timeout_s = 0.2

    with pytest.raises(ProjectGitLockError, match="获取项目 git 锁失败"):
        lock.__enter__()
    assert lock._fcntl.calls == 3
    assert lock._lock_f is None


def test_project_git_flock_default_acquire_budget_is_thirty_seconds(
    monkeypatch, tmp_path,
):
    """未配置时使用独立的短等待预算，坏配置不能把等待恢复成无限。"""
    monkeypatch.delenv("SWARM_GIT_FLOCK_ACQUIRE_TIMEOUT_SEC", raising=False)
    lock = _ProjectGitFlock(tmp_path)
    try:
        assert lock._acquire_timeout_s == 30.0
    finally:
        lock._close_lock_file()

    monkeypatch.setenv("SWARM_GIT_FLOCK_ACQUIRE_TIMEOUT_SEC", "inf")
    fallback = _ProjectGitFlock(tmp_path)
    try:
        assert fallback._acquire_timeout_s == 30.0
    finally:
        fallback._close_lock_file()


def test_standalone_repeated_cancel_is_bounded_by_real_flock_deadline(
    monkeypatch, tmp_path,
):
    """公共 standalone 入口重复取消时仍 join 真实争锁 writer，再释放 ModuleLock。"""
    monkeypatch.setenv("SWARM_GIT_FLOCK_ACQUIRE_TIMEOUT_SEC", "0.12")
    holder_entered = threading.Event()
    release_holder = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer_entered = threading.Event()
    order: list[str] = []

    def holder():
        with _ProjectGitFlock(tmp_path):
            holder_entered.set()
            release_holder.wait(timeout=2)

    class FakeLock:
        def __init__(self, *_args, **_kwargs):
            self.key = "fake"

        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            order.append("module-lock-release")

    class FlockWaitingExecutor:
        def __init__(self, **_kwargs):
            self.execution_log: list[str] = []
            self.phase = type("Phase", (), {"value": "coding"})()

        async def run(self):
            from swarm.infra.cancellation import run_blocking_owned

            def writer():
                writer_started.set()
                try:
                    with _ProjectGitFlock(tmp_path):
                        writer_entered.set()
                finally:
                    order.append("writer-done")
                    writer_done.set()

            await run_blocking_owned(writer, operation="测试真实 flock writer")
            raise AssertionError("争锁超时必须 fail-loud")

    monkeypatch.setattr(worker_runner.store, "get_project", lambda _pid: {
        "id": "project", "path": str(tmp_path),
    })
    monkeypatch.setattr("swarm.tools.paths.set_workspace_root", lambda *_a, **_k: None)
    monkeypatch.setattr("swarm.worker.executor.WorkerExecutor", FlockWaitingExecutor)
    monkeypatch.setattr(redis_client, "ModuleLock", FakeLock)
    monkeypatch.setattr(redis_client, "MultiModuleLock", FakeLock)
    monkeypatch.setattr(redis_client.RenewPacer, "due", lambda *_a, **_k: False)

    hold_thread = threading.Thread(target=holder)
    hold_thread.start()
    assert holder_entered.wait(timeout=1)

    async def scenario():
        outer = asyncio.create_task(worker_runner.run_standalone_worker(
            "standalone-flock-deadline", "project", "desc"))
        assert await asyncio.to_thread(writer_started.wait, 1)
        started = time.monotonic()
        outer.cancel()
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(outer), timeout=0.7)
        assert time.monotonic() - started < 0.7
        assert writer_done.is_set()
        assert not writer_entered.is_set(), "超时后不得迟到进入临界区形成遗留 writer"
        assert order == ["writer-done", "module-lock-release"]

    try:
        asyncio.run(scenario())
    finally:
        release_holder.set()
        hold_thread.join(timeout=1)
        worker_runner._worker_running.discard("standalone-flock-deadline")
        worker_runner._worker_queues.pop("standalone-flock-deadline", None)
