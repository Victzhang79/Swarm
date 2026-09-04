"""主题I M-2（外部深审 MEDIUM）：调度器停机/失主不停在飞任务 → 跨副本双跑。

病根：stop_task_scheduler 只取消消费循环；已派发的在飞任务(_run_with_slot handle，_inflight
计数)继续跑。失主(D38 leadership 丢失)时若不停，新 leader 副本 reconcile 会重新派发同任务 →
双跑（正是 leadership "防双跑" 要杜绝的）。治：停机一并 cancel 在飞派发句柄并有界等收尾、清空
_inflight；DB 非终态由对账恢复（绝不留假终态）。
"""
from __future__ import annotations

import asyncio
import threading

import swarm.brain.runner as runner
import swarm.brain.scheduler as sched


def test_m2_stop_scheduler_cancels_inflight_dispatched():
    async def _scenario():
        cancelled = {"v": False}

        async def _long():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled["v"] = True
                raise

        # 模拟一个已派发的在飞任务：句柄进 _task_handles、task_id 计入 _inflight。
        h = asyncio.create_task(_long())
        await asyncio.sleep(0)  # 让 _long 真正开始 await（可被取消）
        runner._task_handles["t-m2"] = h
        sched._inflight.add("t-m2")
        # 消费循环未起（_consumer_task=None）——本用例只验在飞取消面。
        assert sched._consumer_task is None
        try:
            await sched.stop_task_scheduler()
            assert "t-m2" not in sched._inflight, "停机必须清空在飞额度账（防残留误占并发位）"
            assert h.cancelled() or cancelled["v"], "在飞派发任务句柄必须被 cancel（防跨副本双跑）"
        finally:
            runner._task_handles.pop("t-m2", None)
            sched._inflight.discard("t-m2")

    asyncio.run(_scenario())


def test_m2_zero_drain_keeps_shutdown_marker_until_cancel_handler(monkeypatch):
    async def _scenario():
        seen = []

        async def _long():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                seen.append(runner.is_shutdown_abort("t-zero-drain"))
                raise

        monkeypatch.setenv("SWARM_SCHEDULER_STOP_DRAIN_S", "0")
        h = asyncio.create_task(_long())
        await asyncio.sleep(0)
        runner._task_handles["t-zero-drain"] = h
        sched._inflight.add("t-zero-drain")
        try:
            await sched.stop_task_scheduler()
            await asyncio.gather(h, return_exceptions=True)
            await asyncio.sleep(0)
            assert seen == [True], "取消处理器必须看到停机 marker，否则会误写 CANCELLED"
            assert not runner.is_shutdown_abort("t-zero-drain")
        finally:
            runner._task_handles.pop("t-zero-drain", None)
            runner.clear_shutdown_abort("t-zero-drain")
            sched._inflight.discard("t-zero-drain")

    asyncio.run(_scenario())


def test_m2_stop_scheduler_idempotent_no_inflight():
    """无在飞任务时停机幂等、不抛（应用关闭常态）。"""
    async def _scenario():
        sched._inflight.clear()
        await sched.stop_task_scheduler()  # 绝不抛
        assert sched._consumer_task is None
        assert not sched._inflight

    asyncio.run(_scenario())


def test_m2_shutdown_abort_does_not_write_false_terminal():
    """对抗复核 Finding A：停机中止 → runner CancelledError 处理器【绝不】写 CANCELLED 假终态，
    保留活跃态并 re-raise（交对账恢复）。以 is_shutdown_abort 分流，与人工 cancel 区分。"""
    # mark → is_shutdown_abort True；clear → False。
    runner.mark_shutdown_abort("t-abort")
    assert runner.is_shutdown_abort("t-abort") is True
    runner.clear_shutdown_abort("t-abort")
    assert runner.is_shutdown_abort("t-abort") is False


def test_m2_await_slot_fails_fast_when_stopping(monkeypatch):
    """对抗复核 Finding B：停机进行中 await_execution_slot 立即返 False（不进准入轮询抢空槽）。"""
    async def _scenario():
        monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
        sched._stopping = True
        try:
            got = await sched.await_execution_slot("t-x")
            assert got is sched.ExecutionAdmission.REJECTED_STOPPING
        finally:
            sched._stopping = False

    asyncio.run(_scenario())


def test_m2_resume_background_rejects_while_stopping_and_reverts_claim(monkeypatch):
    """审批已认领但调度器正停机时，不得启动 resume，并须恢复可重试的人工闸状态。"""
    async def _scenario():
        resumed: list[str] = []
        updates: list[tuple[str, dict]] = []

        async def _resume(task_id, *_args, **_kwargs):
            resumed.append(task_id)

        monkeypatch.setattr(runner, "resume_task", _resume)
        monkeypatch.setattr(
            runner.store,
            "update_task",
            lambda task_id, **fields: updates.append((task_id, fields)),
        )
        monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
        sched._stopping = True
        try:
            runner.resume_task_background(
                "t-stop-resume", "accept", revert_status="DELIVERING"
            )
            handle = runner._task_handles["t-stop-resume"]
            await handle

            assert resumed == [], "停机拒绝必须阻止底层 resume 真正启动"
            assert updates == [
                ("t-stop-resume", {"status": "DELIVERING", "resume_saga": {}})
            ], "审批认领态必须回滚，保留用户重试入口"
        finally:
            sched._stopping = False
            runner._task_handles.pop("t-stop-resume", None)

    asyncio.run(_scenario())


def test_m2_planning_resume_rejects_while_stopping_and_reverts_claim(monkeypatch):
    """规划人工闸与交付人工闸必须共享同一停机拒绝语义。"""
    async def _scenario():
        resumed: list[str] = []
        updates: list[tuple[str, dict]] = []

        async def _resume(task_id, *_args, **_kwargs):
            resumed.append(task_id)

        monkeypatch.setattr(runner, "resume_planning", _resume)
        monkeypatch.setattr(
            runner.store,
            "update_task",
            lambda task_id, **fields: updates.append((task_id, fields)),
        )
        sched._stopping = True
        try:
            runner.resume_planning_background(
                "t-stop-plan", {"decision": "approve"}, revert_status="DESIGN_REVIEW"
            )
            handle = runner._task_handles["t-stop-plan"]
            await handle

            assert resumed == [], "停机拒绝必须阻止规划 resume 真正启动"
            assert updates == [
                ("t-stop-plan", {"status": "DESIGN_REVIEW", "resume_saga": {}})
            ], "规划审批认领态必须回滚，保留用户重试入口"
        finally:
            sched._stopping = False
            runner._task_handles.pop("t-stop-plan", None)

    asyncio.run(_scenario())


def test_m2_retry_does_not_turn_stopping_into_no_scheduler_fallback(monkeypatch):
    """停机后的 consumer=False 不是 CLI 模式，retry 不得重置任务后直接执行。"""
    async def _scenario():
        updates: list[dict] = []
        ran: list[str] = []

        monkeypatch.setattr(runner, "can_retry_task", lambda _task_id: (True, ""))
        monkeypatch.setattr(
            runner.store,
            "get_task",
            lambda task_id: {
                "id": task_id,
                "project_id": "p-stop",
                "description": "retry while stopping",
            },
        )
        monkeypatch.setattr(
            runner.store,
            "update_task",
            lambda _task_id, **fields: updates.append(fields),
        )

        async def _run(task_id, *_args, **_kwargs):
            ran.append(task_id)

        monkeypatch.setattr(runner, "run_task", _run)
        monkeypatch.setattr(sched, "is_consumer_running", lambda: False)
        sched._stopping = True
        try:
            accepted = await runner.retry_task("t-stop-retry")
            assert accepted is False
            assert updates == [], "停机拒绝必须发生在终态重置为 SUBMITTED 之前"
            assert ran == [], "停机不能伪装成无调度器 CLI 兜底"
        finally:
            sched._stopping = False
            runner._task_running.discard("t-stop-retry")

    asyncio.run(_scenario())


def test_m2_api_resume_rejects_missing_consumer_instead_of_cli_bypass(monkeypatch):
    """生产后台入口不能把 consumer 意外死亡解释成 standalone 兼容模式。"""
    async def _scenario():
        resumed: list[str] = []
        updates: list[tuple[str, dict]] = []

        async def _resume(task_id, *_args, **_kwargs):
            resumed.append(task_id)

        monkeypatch.setattr(runner, "resume_task", _resume)
        monkeypatch.setattr(
            runner.store,
            "update_task",
            lambda task_id, **fields: updates.append((task_id, fields)),
        )
        monkeypatch.setattr(sched, "is_consumer_running", lambda: False)
        sched._stopping = False

        runner.resume_task_background(
            "t-dead-consumer", "accept", revert_status="DELIVERING"
        )
        handle = runner._task_handles["t-dead-consumer"]
        await handle

        assert resumed == [], "API 入口遇到死 consumer 必须拒绝，不能按 CLI 直跑"
        assert updates == [(
            "t-dead-consumer", {"status": "DELIVERING", "resume_saga": {}}
        )]

    asyncio.run(_scenario())


def test_m2_stop_cancels_scheduler_owned_resume_even_without_capacity_entry(monkeypatch):
    """容量账丢项不能让 scheduler 已接管的 runner handle 逃过 leadership 清扫。"""
    async def _scenario():
        started = asyncio.Event()
        saw_shutdown_marker: list[tuple[bool, str | None]] = []

        async def _resume(task_id, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                saw_shutdown_marker.append((
                    runner.is_shutdown_abort(task_id),
                    runner.shutdown_abort_reason(task_id),
                ))
                raise

        monkeypatch.setattr(runner, "resume_task", _resume)
        monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
        sched._stopping = False
        runner.resume_task_background("t-owned-resume", "accept")
        handle = runner._task_handles["t-owned-resume"]
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # _inflight 是容量账，不应再兼任执行所有权事实源。
        sched._inflight.discard("t-owned-resume")
        try:
            await sched.stop_task_scheduler(reason="leadership_lost")
            await asyncio.sleep(0)
            assert handle.cancelled(), "停机必须取消所有 scheduler-owned runner handle"
            assert saw_shutdown_marker == [
                (True, "leadership_lost")
            ], "取消处理器必须看到 leadership_lost，而非人工取消"
        finally:
            if not handle.done():
                handle.cancel()
                await asyncio.gather(handle, return_exceptions=True)
            runner._task_handles.pop("t-owned-resume", None)
            runner.clear_shutdown_abort("t-owned-resume")
            sched._inflight.discard("t-owned-resume")
            sched._stopping = False

    asyncio.run(_scenario())


def test_m2_rejected_resume_rolls_back_off_event_loop(monkeypatch):
    """拒绝后的 DB 回滚不得同步阻塞 API 事件循环。"""
    async def _scenario():
        event_loop_thread = threading.get_ident()
        update_threads: list[int] = []

        async def _must_not_resume(*_args, **_kwargs):
            raise AssertionError("停机拒绝后不得进入 resume")

        monkeypatch.setattr(runner, "resume_task", _must_not_resume)
        monkeypatch.setattr(
            runner.store,
            "update_task",
            lambda _task_id, **_fields: update_threads.append(threading.get_ident()),
        )
        sched._stopping = True
        try:
            runner.resume_task_background(
                "t-rollback-thread", "accept", revert_status="DELIVERING"
            )
            handle = runner._task_handles["t-rollback-thread"]
            await handle
            assert update_threads and update_threads[0] != event_loop_thread
        finally:
            sched._stopping = False
            runner._task_handles.pop("t-rollback-thread", None)

    asyncio.run(_scenario())


def test_slot_timeout_reverts_resume_and_planning_claims(monkeypatch):
    """容量等待超时必须拒绝两条恢复入口，并把人工闸认领态恢复为可重试状态。"""
    timeout_admission = sched.ExecutionAdmission.REJECTED_TIMEOUT

    async def _scenario():
        resumed: list[str] = []
        updates: list[tuple[str, dict]] = []

        async def _timeout(*_args, **_kwargs):
            return timeout_admission

        async def _must_not_resume(task_id, *_args, **_kwargs):
            resumed.append(task_id)

        monkeypatch.setattr(sched, "await_execution_slot", _timeout)
        monkeypatch.setattr(runner, "resume_task", _must_not_resume)
        monkeypatch.setattr(runner, "resume_planning", _must_not_resume)
        monkeypatch.setattr(
            runner.store,
            "update_task",
            lambda task_id, **fields: updates.append((task_id, fields)),
        )

        runner.resume_task_background(
            "t-timeout-resume", "accept", revert_status="DELIVERING"
        )
        runner.resume_planning_background(
            "t-timeout-plan", {"decision": "approve"}, revert_status="DESIGN_REVIEW"
        )
        await asyncio.gather(
            runner._task_handles["t-timeout-resume"],
            runner._task_handles["t-timeout-plan"],
        )

        assert resumed == []
        assert sorted(updates) == [
            ("t-timeout-plan", {"status": "DESIGN_REVIEW", "resume_saga": {}}),
            ("t-timeout-resume", {"status": "DELIVERING", "resume_saga": {}}),
        ]

    asyncio.run(_scenario())


def test_m2_stop_drain_wait_env_override(monkeypatch):
    monkeypatch.setenv("SWARM_SCHEDULER_STOP_DRAIN_S", "3.5")
    assert sched._stop_drain_wait_s() == 3.5
    monkeypatch.setenv("SWARM_SCHEDULER_STOP_DRAIN_S", "-1")  # 非法 → 默认
    assert sched._stop_drain_wait_s() == 10.0


def test_m2_stop_drain_budget_does_not_wait_for_child_swallowing_cancel(monkeypatch):
    """wait_for(gather) 会等被取消 gather 真结束；吞取消 child 可让停机超出预算。"""
    async def _scenario():
        started = asyncio.Event()
        swallowed = asyncio.Event()
        allow_finish = asyncio.Event()

        async def _stubborn():
            started.set()
            while not allow_finish.is_set():
                try:
                    await allow_finish.wait()
                except asyncio.CancelledError:
                    swallowed.set()
                    continue

        monkeypatch.setenv("SWARM_SCHEDULER_STOP_DRAIN_S", "0.03")
        handle = asyncio.create_task(_stubborn())
        await started.wait()
        sched._owned_execution_handles[handle] = "t-stubborn-drain"
        sched._inflight.add("t-stubborn-drain")
        stopping = asyncio.create_task(sched._cancel_inflight_dispatched())
        done, _pending = await asyncio.wait({stopping}, timeout=0.2)
        try:
            assert swallowed.is_set()
            assert stopping in done, "停机等待必须按预算返回，不能等吞取消 child 真结束"
            assert "t-stubborn-drain" in sched._inflight, "pending straggler 必须继续留额度账"
            assert runner.is_shutdown_abort("t-stubborn-drain")
        finally:
            allow_finish.set()
            await asyncio.gather(handle, return_exceptions=True)
            await asyncio.gather(stopping, return_exceptions=True)
            await asyncio.sleep(0)
            sched._owned_execution_handles.pop(handle, None)
            sched._inflight.discard("t-stubborn-drain")
            runner.clear_shutdown_abort("t-stubborn-drain")

    asyncio.run(_scenario())
    monkeypatch.delenv("SWARM_SCHEDULER_STOP_DRAIN_S", raising=False)
    assert sched._stop_drain_wait_s() == 10.0


if __name__ == "__main__":
    print("run via pytest")
