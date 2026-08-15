#!/usr/bin/env python3
"""任务全生命周期对抗性评审 Batch1 修复回归。

覆盖：URI 凭据脱敏（含 Redis 空用户名）、dispatch contract 重试成功移除 failed_ids、
scheduler SUBMITTED-only 队列重建、reconcile 中断态 checkpoint 探针。纯逻辑/monkeypatch。
"""

from __future__ import annotations


# ── 2nd#5：/api/config URI 内嵌密码脱敏 ──────────────────


def test_mask_postgres_uri_password():
    from swarm.api._shared import _mask_config_dict

    out = _mask_config_dict({"db": {"postgres_uri": "postgresql://swarm:swarm@localhost:5432/swarm"}})
    assert "swarm:swarm@" not in out["db"]["postgres_uri"]
    assert "***" in out["db"]["postgres_uri"]
    assert "localhost:5432/swarm" in out["db"]["postgres_uri"]  # 保留 host/db 供辨识


def test_mask_redis_uri_empty_username():
    """用户报的边角：redis://:password@host 空用户名也必须掩（+→* 修复）。"""
    from swarm.api._shared import _mask_config_dict

    out = _mask_config_dict({"redis_uri": "redis://:supersecret@10.0.0.1:6379/0"})
    assert "supersecret" not in out["redis_uri"]
    assert "***" in out["redis_uri"]


def test_mask_uri_without_credentials_unchanged():
    from swarm.api._shared import _mask_config_dict

    out = _mask_config_dict({"redis_uri": "redis://localhost:6379/0"})
    assert out["redis_uri"] == "redis://localhost:6379/0"


def test_mask_uri_password_containing_at_sign():
    """对抗复核 Finding 5：密码含 @ 也必须全掩（正则版会漏掩 @ss 后缀，urlsplit 版正确）。"""
    from swarm.api._shared import _mask_config_dict

    out = _mask_config_dict({"db": {"postgres_uri": "postgresql://user:p@ss@localhost:5432/db"}})
    masked = out["db"]["postgres_uri"]
    assert "p@ss" not in masked and "@ss@" not in masked
    assert "***" in masked
    assert "localhost:5432/db" in masked


# ── #3：dispatch contract 重试成功从 failed_subtask_ids 移除 ──


def test_dispatch_removes_id_from_failed_on_l1_pass():
    """批25 GS-5w 换锁：原命题「dispatch L1 通过+有 diff 分支必须从 failed_ids 移除该 id（#3，
    否则 contract retry 残留→after_monitor 空转误判失败）」。

    行为锁：真跑 dispatch——st-1 带上轮失败账（failed_subtask_ids）进场，本轮 worker L1 通过
    且有 diff → 返回的 failed_subtask_ids 不再含 st-1。删掉 dispatch 收尾的 remove 块即红。"""
    import asyncio
    from unittest.mock import patch

    from swarm.brain.nodes.dispatch import dispatch
    from swarm.types import (
        Confidence,
        FileScope,
        SubTask,
        SubTaskDifficulty,
        TaskPlan,
        WorkerOutput,
    )

    plan = TaskPlan(subtasks=[SubTask(
        id="st-1", description="do", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["a.x"], readable=[]), depends_on=[],
    )], parallel_groups=[["st-1"]])

    async def _fake_worker(subtask, knowledge_context, **kw):
        return WorkerOutput(subtask_id=subtask.id, diff="+x\n", summary="",
                            confidence=Confidence.HIGH, l1_passed=True)

    state = {
        "task_id": "", "project_id": "", "plan": plan,
        "subtask_results": {}, "dispatch_remaining": ["st-1"],
        # 前提自证：st-1 带着上轮失败残留账进场（contract retry 留下的 failed_ids）
        "failed_subtask_ids": ["st-1"], "knowledge_context": {},
    }
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=_fake_worker):
        out = asyncio.run(dispatch(state))
    assert out["subtask_results"]["st-1"].l1_passed is True, "前提：本轮 L1 必须真通过"
    assert "st-1" not in out["failed_subtask_ids"], (
        "L1 通过+有 diff 必须从 failed_ids 移除（#3 回归）——残留会让 after_monitor 误判失败空转")


# ── #1：scheduler 只重建 SUBMITTED ──────────────────────


def test_scheduler_rebuild_only_submitted(monkeypatch):
    import swarm.brain.scheduler as sched
    from swarm.project import store

    sched._pending_meta.clear()
    sched._inflight.clear()
    # ANALYZING（认领后）等非 SUBMITTED 一律丢弃
    for st in ("ANALYZING", "MONITORING", "MERGING", "CONFIRMING", "DONE"):
        monkeypatch.setattr(store, "get_task", lambda tid, s=st: {
            "id": tid, "project_id": "p", "description": "d", "status": s,
        })
        assert sched._resolve_exec_meta("t") is None, st
    # SUBMITTED 才重建
    monkeypatch.setattr(store, "get_task", lambda tid: {
        "id": tid, "project_id": "p", "description": "d", "status": "SUBMITTED", "auto_accept": False,
    })
    meta = sched._resolve_exec_meta("t")
    assert meta is not None and meta["project_id"] == "p"


# ── 2nd#1：reconcile 中断态 checkpoint 探针 ──────────────


async def test_reconcile_fails_interrupt_without_checkpoint(monkeypatch):
    import swarm.brain.runner as runner
    from swarm.project import store
    import swarm.brain.scheduler as scheduler

    cap = {"updated": [], "audit": []}
    monkeypatch.setattr(store, "list_orphan_candidates", lambda: [
        {"id": "t1", "project_id": "p", "description": "d", "status": "CONFIRMING"},
    ])
    monkeypatch.setattr(store, "update_task", lambda tid, **kw: cap["updated"].append((tid, kw)))
    monkeypatch.setattr(store, "append_task_audit", lambda tid, **kw: cap["audit"].append((tid, kw)))
    monkeypatch.setattr(scheduler, "is_task_claimed", lambda tid: False)
    runner._task_running.clear()

    async def _no_ckpt(tid):
        return False  # checkpoint 丢失

    monkeypatch.setattr(runner, "_has_pending_checkpoint", _no_ckpt)
    stats = await runner.reconcile_orphan_tasks()
    assert stats["failed"] == 1
    assert stats["resumed_interrupt"] == 0
    assert cap["updated"] and cap["updated"][0][1]["status"] == "FAILED"
    assert any(a[1]["event"] == "checkpoint_missing" for a in cap["audit"])


async def test_reconcile_keeps_interrupt_with_checkpoint(monkeypatch):
    import swarm.brain.runner as runner
    from swarm.project import store
    import swarm.brain.scheduler as scheduler

    cap = {"updated": []}
    monkeypatch.setattr(store, "list_orphan_candidates", lambda: [
        {"id": "t2", "project_id": "p", "description": "d", "status": "DELIVERING"},
    ])
    monkeypatch.setattr(store, "update_task", lambda tid, **kw: cap["updated"].append(tid))
    monkeypatch.setattr(store, "append_task_audit", lambda tid, **kw: None)
    monkeypatch.setattr(scheduler, "is_task_claimed", lambda tid: False)
    runner._task_running.clear()

    async def _has_ckpt(tid):
        return True

    monkeypatch.setattr(runner, "_has_pending_checkpoint", _has_ckpt)
    stats = await runner.reconcile_orphan_tasks()
    assert stats["resumed_interrupt"] == 1
    assert stats["failed"] == 0
    assert cap["updated"] == []  # 保留不动


# ── 3rd-P1a：CONFIRM 修订把 feedback 带进 replan_feedback ──


def test_confirm_revise_carries_feedback_to_replan(monkeypatch):
    """批25 GS-5w 换锁：原命题「confirm 节点 REVISE 分支必须把 payload.feedback 注入
    replan_feedback，供 PLAN 定向重规划（3rd-P1a）」。

    行为锁：真调 confirm_plan——interrupt 返回 REVISE+feedback → 返回补丁的
    replan_feedback == 用户反馈（去首尾空白）。删掉 REVISE 注入块即红。"""
    import swarm.brain.nodes as nodes
    from swarm.types import Complexity, HumanDecision

    monkeypatch.delenv("SWARM_AUTO_ACCEPT", raising=False)  # 防 .env 残留把分支拐进 auto_accept
    monkeypatch.setattr(
        nodes, "interrupt",
        lambda payload: {"decision": "revise", "feedback": "  把登录改成 OAuth  "})
    out = nodes.confirm_plan({
        "task_id": "t1", "task_description": "d", "plan_valid": True,
        "complexity": Complexity.MEDIUM, "plan": None,
    })
    assert out.get("human_decision") == HumanDecision.REVISE, "前提：必须真走到 REVISE 分支"
    assert out.get("replan_feedback") == "把登录改成 OAuth", (
        "REVISE 必须把 feedback 注入 replan_feedback（3rd-P1a 回归）——否则 confirm 修订退化成盲重规划")


# ── 2nd#2：ModuleLock renew 失败 → fail-fast 中止 ──────────


async def test_stream_loop_aborts_on_lock_lost(monkeypatch):
    """批25 GS-5w 换锁：原命题「renew() 返回 False（Redis 侧失锁）→ raise TaskLockLost，
    fail-fast 中止防同模块并发写（2nd#2）」。

    行为锁：真跑 _stream_brain_events——假锁 renew 恒 False + pacer 恒 due →
    首个图事件即 emit lock_lost 并抛 TaskLockLost。删掉循环顶的失锁检查块即红。"""
    import asyncio

    import pytest

    import swarm.brain.runner as runner
    import swarm.infra.redis_client as rc
    from swarm.models import ledger as _ledger
    from swarm.models import usage_tracker

    class _LostLock:
        key = "p/m"
        ttl_sec = 3600

        def __init__(self):
            self.renew_calls = 0

        def renew(self):
            self.renew_calls += 1
            return False  # 前提：Redis 侧锁已丢失

    class _AlwaysDuePacer:
        def due(self, lock, now=None):
            return True  # 绕过降频：每个事件都真查 renew（被测语义本身与降频无关）

    class _FakeGraph:
        async def astream_events(self, *a, **k):
            yield {"event": "on_chain_start", "name": "LangGraph", "data": {}}

    monkeypatch.setattr(rc, "RenewPacer", lambda: _AlwaysDuePacer())
    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _FakeGraph())
    monkeypatch.setattr(runner.store, "get_task", lambda tid: {"id": tid, "thread_id": tid})
    # 用量/账本登记是旁路 bookkeeping，与锁语义无关——隔掉全局态
    monkeypatch.setattr(usage_tracker, "set_current_task", lambda tid: None)
    monkeypatch.setattr(_ledger, "attach", lambda *a, **k: None)

    emitted: list[dict] = []

    class _Topic:
        def publish(self, event):
            emitted.append(event)

    lock = _LostLock()
    task_id = "t-gs5w-locklost"
    try:
        with pytest.raises(runner.TaskLockLost):
            await runner._stream_brain_events(task_id, {}, _Topic(), lock_holder={"lock": lock})
    finally:
        runner._stop_watchdog(task_id)  # 异常路径看门狗由调用方 finally 停；测试里自己收
        await asyncio.sleep(0)  # 让取消落定，防 pending task 告警
    assert lock.renew_calls >= 1, "前提：失锁判定必须真实调用过 renew"
    assert any(e.get("step") == "lock_lost" for e in emitted), (
        "失锁必须先 emit lock_lost 再抛（2nd#2 回归）")
    assert issubclass(runner.TaskLockLost, Exception)


def test_renew_tolerates_transient_then_aborts(monkeypatch):
    """对抗复核 Finding 4a：renew 瞬时错误（异常）容忍到阈值前返 True（不误杀长任务），
    连续超阈值才返 False；确认被抢（Lua 返 0）立即返 False。"""
    import swarm.infra.redis_client as rc

    monkeypatch.setenv("SWARM_LOCK_RENEW_TRANSIENT_MAX", "3")

    class _BoomRedis:
        def eval(self, *a, **k):
            raise ConnectionError("redis blip")

    monkeypatch.setattr(rc, "get_redis", lambda: _BoomRedis())
    lock = rc.ModuleLock("p", "m")
    lock._held = True
    lock._redis_held = True  # H-2 后：只有 Redis-held 锁 renew 才走 Lua/瞬时容忍逻辑
    # 前 2 次瞬时失败 → 容忍（True）；第 3 次达阈值 → 判失锁（False）
    assert lock.renew() is True
    assert lock.renew() is True
    assert lock.renew() is False


def test_renew_confirmed_loss_aborts_immediately(monkeypatch):
    """Lua 返回 0（value 已非本 token=被抢/过期）→ 立即判失锁，不走瞬时容忍。"""
    import swarm.infra.redis_client as rc

    class _StolenRedis:
        def eval(self, *a, **k):
            return 0  # 锁已不是自己的

    monkeypatch.setattr(rc, "get_redis", lambda: _StolenRedis())
    lock = rc.ModuleLock("p", "m")
    lock._held = True
    lock._redis_held = True  # H-2 后：只有 Redis-held 锁 renew 才走 Lua（确认失锁判定）
    assert lock.renew() is False  # 立即，不容忍


def test_renew_memory_fallback_never_aborts(monkeypatch):
    """Redis 未启用（get_redis 返 None）→ renew 恒 True（单进程无跨进程互斥意义，不误杀）。"""
    import swarm.infra.redis_client as rc

    monkeypatch.setattr(rc, "get_redis", lambda: None)
    lock = rc.ModuleLock("p", "m")
    lock._held = True
    assert lock.renew() is True


def test_learn_success_kb_trigger_uses_ok_not_committed(monkeypatch):
    """批25 GS-5w 换锁：原命题「KB 触发条件用 commit 的 ok（含"无改动可提交"的合法 no-op），
    非 committed——否则 commit 报 no-op 时静默漏掉整任务 KB 更新（Finding 2）」。

    行为锁：真跑 learn_success，交付 commit 返 ok=True/committed=False →
    schedule_incremental_update 必须被调。触发条件改回 committed（或删掉触发块）即红。"""
    import asyncio

    import swarm.brain.learn_store as learn_store
    import swarm.knowledge.hooks as hooks
    from swarm.brain import nodes
    from swarm.types import Complexity

    kb_calls: list = []
    monkeypatch.setattr(hooks, "schedule_incremental_update",
                        lambda *a, **k: kb_calls.append((a, k)))
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: "/tmp/fake-proj")

    async def _fake_deliver(proj_path, merged_diff, base_commit, out_files, task_id):
        return {
            "ap": {"ok": True, "applied": ["a.py"], "failed": []},
            "out_files": ["a.py"], "wm": {},
            # 关键前提：ok=True 但 committed=False（nothing-to-commit 合法 no-op）
            "commit": {"ok": True, "committed": False, "reason": "nothing to commit"},
        }

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _fake_deliver)

    async def _fake_persist(state, parsed):
        return {"persisted": False}

    monkeypatch.setattr(learn_store, "persist_learn_success", _fake_persist)

    out = asyncio.run(nodes.learn_success({
        "task_id": "t1", "project_id": "p1", "task_description": "d",
        "merged_diff": "--- a/a.py\n+++ b/a.py\n@@ +x\n",
        "complexity": Complexity.SIMPLE,  # SIMPLE 路径不走 LLM，聚焦交付→KB 触发面
    }))
    assert out.get("learned") is True, "前提：learn_success 必须真走完"
    assert len(kb_calls) == 1, (
        "commit ok=True（含 no-op）必须触发 KB 增量索引——改回 committed 条件即静默漏更"
        "（Finding 2 回归）")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
