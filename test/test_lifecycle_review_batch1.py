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


def test_dispatch_removes_id_from_failed_on_l1_pass_source():
    import inspect

    from swarm.brain.nodes import dispatch

    src = inspect.getsource(dispatch)
    # L1 通过 + 有 diff 分支必须 remove(subtask.id)，否则 contract retry 空转
    assert "failed_ids.remove(subtask.id)" in src, "dispatch L1 通过后未从 failed_ids 移除（#3 回归）"


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


def test_confirm_revise_carries_feedback_to_replan():
    """confirm 节点 REVISE 分支必须把 payload.feedback 注入 replan_feedback（供 PLAN 定向重规划）。"""
    import inspect

    from swarm.brain import nodes

    src = inspect.getsource(nodes.confirm_plan)
    assert '_patch_out["replan_feedback"] = _fb' in src, \
        "confirm_plan REVISE 未把 feedback 注入 replan_feedback（3rd-P1a 回归）"
    assert 'decision.get("feedback")' in src


# ── 2nd#2：ModuleLock renew 失败 → fail-fast 中止 ──────────


def test_stream_loop_aborts_on_lock_lost():
    """renew() 返回 False（Redis 侧失锁）→ raise TaskLockLost，防同模块并发写。"""
    import inspect

    from swarm.brain import runner

    src = inspect.getsource(runner._stream_brain_events)
    # D14 后 renew 经 RenewPacer 降频 + asyncio.to_thread 卸线程池，但"renew False → TaskLockLost"
    # 的 fail-fast 语义必须保留。
    assert "module_lock.renew" in src, "renew 失败未被检查（2nd#2 回归）"
    assert "TaskLockLost" in src
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


def test_learn_success_kb_trigger_uses_ok_not_committed():
    """对抗复核 Finding 2：KB 触发条件用 _c.get('ok')（含 no-op），非 committed——否则 commit
    报'无改动可提交'时静默漏掉整任务 KB 更新。"""
    import inspect

    from swarm.brain import nodes

    src = inspect.getsource(nodes.learn_success)
    # 触发块用 if _c.get("ok"):（而非嵌在 if _c.get("committed") 里）
    assert 'if _c.get("ok"):' in src, "KB 触发未改用 ok 条件（Finding 2 回归）"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
