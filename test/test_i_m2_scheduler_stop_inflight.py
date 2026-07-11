"""主题I M-2（外部深审 MEDIUM）：调度器停机/失主不停在飞任务 → 跨副本双跑。

病根：stop_task_scheduler 只取消消费循环；已派发的在飞任务(_run_with_slot handle，_inflight
计数)继续跑。失主(D38 leadership 丢失)时若不停，新 leader 副本 reconcile 会重新派发同任务 →
双跑（正是 leadership "防双跑" 要杜绝的）。治：停机一并 cancel 在飞派发句柄并有界等收尾、清空
_inflight；DB 非终态由对账恢复（绝不留假终态）。
"""
from __future__ import annotations

import asyncio

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
            assert got is False, "停机中 resume 准入立即让位，不占位起新执行"
        finally:
            sched._stopping = False

    asyncio.run(_scenario())


def test_m2_stop_drain_wait_env_override(monkeypatch):
    monkeypatch.setenv("SWARM_SCHEDULER_STOP_DRAIN_S", "3.5")
    assert sched._stop_drain_wait_s() == 3.5
    monkeypatch.setenv("SWARM_SCHEDULER_STOP_DRAIN_S", "-1")  # 非法 → 默认
    assert sched._stop_drain_wait_s() == 10.0
    monkeypatch.delenv("SWARM_SCHEDULER_STOP_DRAIN_S", raising=False)
    assert sched._stop_drain_wait_s() == 10.0


if __name__ == "__main__":
    print("run via pytest")
