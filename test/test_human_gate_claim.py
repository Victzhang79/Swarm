#!/usr/bin/env python3
"""P1-A：审批端点原子认领（claim_human_gate）幂等 + 前置态校验单测。

修前：approve/revise/reject/clarify/review-design 均只 404+perm 后盲 resume，无当前态校验、
无去重 → 双击 approve 会重复 apply diff + 重复触发 resume + 对非审核态任务发 spurious resume。
修后：单条条件 UPDATE 原子认领——仅当任务处于对应人工闸态才推进（一次成功），重复提交匹配 0 行
→ None → 端点走幂等无副作用分支。

集成（需 PG，_pg_available 守卫）：验证原子性与态校验。
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig
from swarm.project import store
from swarm.task_states import PLAN_RESULT_REVIEW_STATES


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="PG 不可达")


def _mk_task(status: str) -> tuple[str, str]:
    store.ensure_tables()
    pid = f"p-{uuid.uuid4().hex[:8]}"
    tid = f"t-{uuid.uuid4().hex[:8]}"
    store.create_project(pid, name="claim-test", path=f"/tmp/{pid}")
    store.create_task(tid, pid, "claim test")
    store.update_task(tid, status=status)
    return pid, tid


def _cleanup(pid: str) -> None:
    try:
        store.delete_project(pid)
    except Exception:
        pass


def test_claim_wins_once_then_second_is_none():
    """CONFIRMING → 首次认领成功（状态推进 ANALYZING + 记 ACCEPT）；再认领 → None（幂等）。"""
    pid, tid = _mk_task("CONFIRMING")
    try:
        first = store.claim_human_gate(tid, PLAN_RESULT_REVIEW_STATES, "ANALYZING", human_decision="ACCEPT")
        assert first is not None
        assert first["status"] == "ANALYZING"
        assert first["human_decision"] == "ACCEPT"
        second = store.claim_human_gate(tid, PLAN_RESULT_REVIEW_STATES, "ANALYZING", human_decision="ACCEPT")
        assert second is None, "第二次认领必须失败（防双击重复副作用）"
    finally:
        _cleanup(pid)


def test_claim_rejects_wrong_state():
    """任务在活跃执行态（MONITORING，非审核态）→ 认领 None，不推进、不触发副作用。"""
    pid, tid = _mk_task("MONITORING")
    try:
        assert store.claim_human_gate(tid, PLAN_RESULT_REVIEW_STATES, "ANALYZING", human_decision="ACCEPT") is None
        # 状态未被篡改
        assert store.get_task(tid)["status"] == "MONITORING"
    finally:
        _cleanup(pid)


def test_claim_rejects_terminal_state():
    pid, tid = _mk_task("DONE")
    try:
        assert store.claim_human_gate(tid, PLAN_RESULT_REVIEW_STATES, "ANALYZING", human_decision="ACCEPT") is None
        assert store.get_task(tid)["status"] == "DONE"
    finally:
        _cleanup(pid)


def test_claim_delivering_state_allowed():
    """DELIVERING（结果审核）也在 approve/revise/reject 放行集内。"""
    pid, tid = _mk_task("DELIVERING")
    try:
        claimed = store.claim_human_gate(tid, PLAN_RESULT_REVIEW_STATES, "IN_REVISION", human_decision="REVISE")
        assert claimed is not None
        assert claimed["status"] == "IN_REVISION"
        assert claimed["human_decision"] == "REVISE"
    finally:
        _cleanup(pid)


def test_claim_single_state_clarify():
    """clarify 端点只认 CLARIFYING；DESIGN_REVIEW 任务不被 clarify 认领（态隔离）。"""
    pid, tid = _mk_task("DESIGN_REVIEW")
    try:
        assert store.claim_human_gate(tid, {"CLARIFYING"}, "ANALYZING") is None
        # 但 review-design 的态集能认领它
        claimed = store.claim_human_gate(tid, {"DESIGN_REVIEW"}, "ANALYZING")
        assert claimed is not None and claimed["status"] == "ANALYZING"
    finally:
        _cleanup(pid)


def test_claim_without_human_decision_preserves_column():
    """clarify/review-design 认领不带 human_decision → 该列保持原值（不污染审批决策语义）。"""
    pid, tid = _mk_task("CLARIFYING")
    try:
        store.update_task(tid, human_decision="")  # 基线空
        claimed = store.claim_human_gate(tid, {"CLARIFYING"}, "ANALYZING")
        assert claimed is not None
        assert claimed["status"] == "ANALYZING"
        assert (claimed.get("human_decision") or "") == ""
    finally:
        _cleanup(pid)


def test_resume_reverts_status_on_module_lock_busy(monkeypatch):
    """F1 对抗复核治本：认领已把状态推 ANALYZING，若 resume 因模块锁占用未能开跑，
    须回滚到原审核态，否则任务卡 ANALYZING 无 resume、用户无法再点通过。"""
    import asyncio

    from swarm.brain import runner

    reverted = {}

    def _fake_update(task_id, **kw):
        if "status" in kw:
            reverted["status"] = kw["status"]
        return {"id": task_id, **kw}

    monkeypatch.setattr(runner.store, "get_task", lambda tid: {"id": tid, "project_id": "p1", "status": "ANALYZING"})
    monkeypatch.setattr(runner.store, "update_task", _fake_update)
    monkeypatch.setattr(runner, "_set_workspace", lambda pid: None)

    class _BusyLock:
        def __init__(self, *a, **k): ...
        def acquire(self): return False
        def release(self): ...

    # 模块锁始终占用 → resume 早退
    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", _BusyLock)
    monkeypatch.setattr("swarm.infra.redis_client.TaskQueue.enqueue", staticmethod(lambda *a, **k: None))
    runner._task_running.discard("t-lock")

    asyncio.run(runner.resume_task("t-lock", "accept", revert_status="DELIVERING"))
    assert reverted.get("status") == "DELIVERING", "锁占用早退必须回滚认领状态到原审核态"
    assert "t-lock" not in runner._task_running


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
