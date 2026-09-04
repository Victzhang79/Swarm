"""主题I M-5（外部深审 MEDIUM）：审批恢复绕过 max_concurrent 准入 → 批量审批无界超卖。

初始任务经 submit_task→队列→消费循环（len(_inflight)<max_concurrent 门控）。但图 interrupt
返回时释放了调度槽，任务在等人审批期间不占额度；审批恢复 resume_task_background 直接
asyncio.create_task→无门控。跨项目批量审批可瞬时拉起 N 个 resume 无界超卖模型/线程/沙箱。
治：await_execution_slot 让 resume 等到有空位（与初始任务共享 _inflight 账=统一天花板）再跑，
release_execution_slot 对称释放。消费器未运行（CLI/测试）不门控；等待超时显式拒绝并回滚认领态。
"""
from __future__ import annotations

import asyncio

import pytest

import swarm.brain.scheduler as sched


@pytest.fixture(autouse=True)
def _snap_inflight():
    snap = set(sched._inflight)
    try:
        yield
    finally:
        sched._inflight.clear()
        sched._inflight.update(snap)


def test_m5_no_gate_when_consumer_not_running(monkeypatch):
    monkeypatch.setattr(sched, "is_consumer_running", lambda: False)
    got = asyncio.run(sched.await_execution_slot("t1", allow_no_scheduler=True))
    assert got is sched.ExecutionAdmission.BYPASS_NO_SCHEDULER


def test_m5_production_entry_rejects_when_consumer_not_running(monkeypatch):
    monkeypatch.setattr(sched, "is_consumer_running", lambda: False)
    got = asyncio.run(sched.await_execution_slot("t1"))
    assert got is sched.ExecutionAdmission.REJECTED_UNAVAILABLE


def test_m5_admits_and_releases_when_slot_free(monkeypatch):
    monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
    monkeypatch.setattr(sched, "_max_concurrent", lambda: 2)
    sched._inflight.clear()
    assert asyncio.run(sched.await_execution_slot("t1")) is sched.ExecutionAdmission.SLOTTED
    assert "t1" in sched._inflight, "占位=计入统一天花板"
    sched.release_execution_slot("t1")
    assert "t1" not in sched._inflight


def test_m5_waits_when_full_then_admits_on_release(monkeypatch):
    monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
    monkeypatch.setattr(sched, "_max_concurrent", lambda: 1)
    sched._inflight.clear()
    sched._inflight.add("busy")  # 满额

    async def body():
        task = asyncio.create_task(sched.await_execution_slot("t2"))
        await asyncio.sleep(0.4)
        assert not task.done(), "满额时 resume 必须等待（不无界超卖）"
        sched.release_execution_slot("busy")  # 腾出空位
        got = await asyncio.wait_for(task, timeout=3.0)
        assert got is sched.ExecutionAdmission.SLOTTED and "t2" in sched._inflight
        sched.release_execution_slot("t2")

    asyncio.run(body())


def test_m5_no_overshoot_shared_ceiling(monkeypatch):
    """两个并发 resume 共享天花板：max=1 时只放 1 个进，另一个等。"""
    monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
    monkeypatch.setattr(sched, "_max_concurrent", lambda: 1)
    sched._inflight.clear()

    async def body():
        a = asyncio.create_task(sched.await_execution_slot("a"))
        b = asyncio.create_task(sched.await_execution_slot("b"))
        await asyncio.sleep(0.4)
        done = [t for t in (a, b) if t.done()]
        assert len(done) == 1, "max=1 → 只有一个被准入，另一个等（不超卖）"
        assert len(sched._inflight) == 1
        # 释放被准入者 → 另一个进
        winner = "a" if "a" in sched._inflight else "b"
        sched.release_execution_slot(winner)
        await asyncio.wait_for(asyncio.gather(a, b), timeout=3.0)
        assert len(sched._inflight) == 1
        sched._inflight.clear()

    asyncio.run(body())


def test_m5_leadership_await_cannot_split_capacity_check_from_claim(monkeypatch):
    """最终验主即使真实让出事件循环，容量复核与占槽也不能被并发任务穿透。"""
    monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
    monkeypatch.setattr(sched, "_max_concurrent", lambda: 1)
    sched._inflight.clear()

    async def _yielding_leadership_check():
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(sched, "verify_local_execution_leadership", _yielding_leadership_check)

    async def body():
        a = asyncio.create_task(sched.await_execution_slot("lease-a"))
        b = asyncio.create_task(sched.await_execution_slot("lease-b"))
        await asyncio.sleep(0.05)
        assert len([task for task in (a, b) if task.done()]) == 1
        assert len(sched._inflight) == 1

        winner = next(iter(sched._inflight))
        sched.release_execution_slot(winner)
        results = await asyncio.wait_for(asyncio.gather(a, b), timeout=3.0)
        assert results == [sched.ExecutionAdmission.SLOTTED] * 2
        assert len(sched._inflight) == 1
        sched._inflight.clear()

    asyncio.run(body())


def test_m5_duplicate_same_task_no_slot_corruption(monkeypatch):
    """hunter F1：同 task_id 重复 resume——第二次返回 False（不重复占位），其 release 不得
    误删仍在跑的第一次的槽位（_inflight 是 set 非 refcount）。"""
    monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
    monkeypatch.setattr(sched, "_max_concurrent", lambda: 5)
    sched._inflight.clear()
    a_slotted = asyncio.run(sched.await_execution_slot("dup"))
    assert a_slotted is sched.ExecutionAdmission.SLOTTED and "dup" in sched._inflight
    # 第二次同 task：返回 ALREADY_CLAIMED（已在账），不重复占位
    b_slotted = asyncio.run(sched.await_execution_slot("dup"))
    assert b_slotted is sched.ExecutionAdmission.ALREADY_CLAIMED
    # 模拟 B 完成：只有 _slotted 为 True 才 release——B 不 release，A 的槽保住
    if b_slotted is sched.ExecutionAdmission.SLOTTED:
        sched.release_execution_slot("dup")
    assert "dup" in sched._inflight, "B 未占位故不 release，A(仍在跑)的槽位必须保住"
    sched.release_execution_slot("dup")  # A 完成
    assert "dup" not in sched._inflight


def test_m5_timeout_rejects_all_waiters_without_overshooting_ceiling(monkeypatch):
    """N 个 waiter 同时超时也必须全拒绝，绝不能把 max_concurrent 当软上限。"""
    monkeypatch.setattr(sched, "is_consumer_running", lambda: True)
    monkeypatch.setattr(sched, "_max_concurrent", lambda: 1)
    monkeypatch.setenv("SWARM_RESUME_SLOT_WAIT_S", "0.01")
    sched._inflight.clear()
    sched._inflight.add("stuck")  # 永不释放

    async def body():
        results = await asyncio.gather(*(
            sched.await_execution_slot(f"waiter-{i}") for i in range(12)
        ))
        assert results == [sched.ExecutionAdmission.REJECTED_TIMEOUT] * 12
        assert sched._inflight == {"stuck"}, "超时 waiter 不得写入容量账或打穿上限"

    asyncio.run(body())


if __name__ == "__main__":
    print("run via pytest")
