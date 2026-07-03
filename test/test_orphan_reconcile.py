#!/usr/bin/env python3
"""P0-A：reconcile_orphan_tasks 启动对账态分治单测。

修前：on_startup 只清沙箱、从不扫 task_records → 重启后非终态任务停在假"进行中"，
既不续跑也不失败（健康探针假绿之外的第二个假绿）。
修后：按态类别分治——中断挂起态保留待人工 resume / SUBMITTED 重入队自动恢复 /
其余活跃执行态 fail-closed FAILED(orphaned_on_restart) + 释放资源。

monkeypatch store/scheduler/sandbox，聚焦分类逻辑（真正易错处），不依赖真 DB。
"""

from __future__ import annotations

import swarm.brain.runner as runner


def _rec(tid, status, **kw):
    base = {
        "id": tid, "project_id": f"proj-{tid}", "description": f"desc {tid}",
        "status": status, "auto_accept": kw.get("auto_accept", False),
        "queue_priority": kw.get("queue_priority", "normal"),
    }
    return base


async def _run_reconcile(monkeypatch, candidates):
    """跑 reconcile，捕获所有副作用调用，返回 (stats, captures)。"""
    from swarm.project import store
    import swarm.brain.scheduler as scheduler
    import swarm.worker.sandbox as sandbox_mod

    cap = {"updated": [], "audit": [], "submitted": [], "killed": []}

    monkeypatch.setattr(store, "list_orphan_candidates", lambda: list(candidates))
    monkeypatch.setattr(store, "update_task", lambda tid, **kw: cap["updated"].append((tid, kw)))
    monkeypatch.setattr(store, "append_task_audit", lambda tid, **kw: cap["audit"].append((tid, kw)))
    monkeypatch.setattr(scheduler, "submit_task", lambda tid, pid, desc, **kw: cap["submitted"].append((tid, pid, desc, kw)))

    class _FakeMgr:
        def kill_by_task(self, tid):
            cap["killed"].append(tid)
            return 1

    monkeypatch.setattr(sandbox_mod, "get_sandbox_manager", lambda: _FakeMgr())

    # 2nd#1：默认桩 checkpoint 存在（中断态保留路径）；专门测 checkpoint 丢失的用例自行覆盖。
    async def _has_ckpt(tid):
        return True

    monkeypatch.setattr(runner, "_has_pending_checkpoint", _has_ckpt)
    runner._task_running.clear()

    stats = await runner.reconcile_orphan_tasks()
    return stats, cap


async def test_interrupt_states_are_kept_not_failed(monkeypatch):
    cands = [_rec(s, s) for s in ("CONFIRMING", "DELIVERING", "CLARIFYING", "DESIGN_REVIEW")]
    stats, cap = await _run_reconcile(monkeypatch, cands)
    assert stats["resumed_interrupt"] == 4
    assert stats["failed"] == 0
    # 保留：不改 DB 状态、不释放沙箱
    assert cap["updated"] == []
    assert cap["killed"] == []
    # 有对账留痕
    events = {a[1]["event"] for a in cap["audit"]}
    assert events == {"recovered_interrupt"}


async def test_submitted_is_requeued(monkeypatch):
    cands = [_rec("s1", "SUBMITTED", auto_accept=True, queue_priority="urgent")]
    stats, cap = await _run_reconcile(monkeypatch, cands)
    assert stats["requeued"] == 1
    assert stats["failed"] == 0
    assert len(cap["submitted"]) == 1
    tid, pid, desc, kw = cap["submitted"][0]
    assert tid == "s1" and pid == "proj-s1"
    # 持久化的执行 meta 被透传（auto_accept + priority）
    assert kw["auto_accept"] is True
    assert kw["priority"] == "urgent"
    # SUBMITTED 不失败、不释放沙箱
    assert cap["updated"] == []
    assert cap["killed"] == []


async def test_active_execution_states_fail_closed_and_release(monkeypatch):
    active = ("ANALYZING", "PLANNING", "DISPATCHING", "MONITORING",
              "HANDLING_FAILURE", "MERGING", "VERIFYING_L2", "VERIFYING_L3",
              "IN_REVISION", "LEARNING_SUCCESS", "LEARNING_FAILURE")
    cands = [_rec(s, s) for s in active]
    stats, cap = await _run_reconcile(monkeypatch, cands)
    assert stats["failed"] == len(active)
    assert stats["resumed_interrupt"] == 0 and stats["requeued"] == 0
    # 每条都标 FAILED + 释放沙箱 + 留痕
    assert {u[0] for u in cap["updated"]} == set(active)
    assert all(u[1]["status"] == "FAILED" for u in cap["updated"])
    assert set(cap["killed"]) == set(active)
    assert {a[1]["event"] for a in cap["audit"]} == {"orphaned_on_restart"}


async def test_task_running_in_process_is_skipped(monkeypatch):
    from swarm.project import store
    import swarm.brain.scheduler as scheduler

    cap = {"updated": [], "submitted": []}
    monkeypatch.setattr(store, "list_orphan_candidates", lambda: [_rec("live", "MONITORING")])
    monkeypatch.setattr(store, "update_task", lambda tid, **kw: cap["updated"].append(tid))
    monkeypatch.setattr(scheduler, "submit_task", lambda *a, **k: cap["submitted"].append(a))
    runner._task_running.clear()
    runner._task_running.add("live")  # 本进程正在跑 → 非孤儿
    try:
        stats = await runner.reconcile_orphan_tasks()
    finally:
        runner._task_running.discard("live")
    assert stats["skipped_running"] == 1
    assert stats["failed"] == 0
    assert cap["updated"] == []


async def test_task_claimed_in_scheduler_inflight_is_skipped(monkeypatch):
    """F3：已出队进调度器并发槽（_inflight）但 run_task 尚未把 task 加进 _task_running 的窗口，
    reconcile 也必须跳过——否则虚假重入队 + 冗余 Redis 项。"""
    from swarm.project import store
    import swarm.brain.scheduler as scheduler

    cap = {"submitted": [], "updated": []}
    monkeypatch.setattr(store, "list_orphan_candidates", lambda: [_rec("claimed", "SUBMITTED")])
    monkeypatch.setattr(scheduler, "submit_task", lambda *a, **k: cap["submitted"].append(a))
    monkeypatch.setattr(store, "update_task", lambda tid, **kw: cap["updated"].append(tid))
    runner._task_running.clear()
    scheduler._inflight.clear()
    scheduler._inflight.add("claimed")  # 已在并发槽（但未进 _task_running）
    try:
        stats = await runner.reconcile_orphan_tasks()
    finally:
        scheduler._inflight.discard("claimed")
    assert stats["skipped_running"] == 1
    assert stats["requeued"] == 0
    assert cap["submitted"] == []  # 不虚假重入队


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
