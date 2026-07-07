"""P1-11 API 交付/事务/流 D17/D18/D22 治本回归（DEEP_READ_REGISTER_2026-07-07）。

D17 approve 隐式 apply 失败被吞：apply 失败（无论显式/隐式）一律阻断 accept 推进——
     422 + 回滚认领状态，不触发 resume（任务留在可重试/待人工态）。
D18 cancel 后 SSE/WS 流永不终止：break 集合补 "cancelled"（SSE/WS 对称）；
     result 死协议治本：终态载荷并入 complete 事件（CLI 已原生消费 complete.result，
     WebUI 靠 complete 后 REST 重载，破坏最小），删除独立 step:"result" 发布。
D22 创建链路部分写入无回滚：task 侧 create_task 单条 INSERT 落全部初始 meta
     （status/thread_id/auto_accept/queue_priority），消灭两步窗口；project 侧
     set_project_member 失败补偿删除刚建项目（补偿失败 error 留痕）。

API 测试沿用仓内既有模式（test_cancel_and_logstream：TestClient + patch swarm.api.app.store；
conftest 默认 SWARM_RBAC_ENABLED=false → 匿名 admin 放行）。
DB 测试沿用真实 PG + _test_ 前缀 + finally 清理（PG 不可达 skip）。
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import MagicMock

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


_PG_OK = _pg_available()
requires_pg = pytest.mark.skipif(not _PG_OK, reason="PG 不可达")


# ══════════════════════════════════════════════════════════════════
# D17 — approve：apply 失败（显式/隐式）一律阻断 accept
# ══════════════════════════════════════════════════════════════════

_TASK = {
    "id": "t-d17",
    "project_id": "p1",
    "status": "DELIVERING",
    "merged_diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
}


def _approve_client(monkeypatch, *, apply_ok: bool):
    """搭好 approve 端点的最小 mock 环境，返回 (client, store_mock, resume_spy)。"""
    import importlib
    _app = importlib.import_module("swarm.api.app")  # api/__init__ 遮蔽 app 子模块，须 importlib
    import swarm.brain.runner as runner
    from fastapi.testclient import TestClient

    store = MagicMock()
    task = dict(_TASK)
    store.get_task.return_value = task
    claimed = dict(task, status="ANALYZING", human_decision="ACCEPT")
    store.claim_human_gate.return_value = claimed
    store.get_project.return_value = {"id": "p1", "path": "/tmp/_test_d17_proj"}
    store.update_task.return_value = None
    store.create_notification.return_value = None
    monkeypatch.setattr(_app, "store", store)

    # 隐式 apply 条件：非 sandbox_first + 有 merged_diff
    cfg = _app.get_config()
    monkeypatch.setattr(cfg.sandbox, "sandbox_first", False)

    import swarm.project.diff_apply as diff_apply
    monkeypatch.setattr(
        diff_apply, "apply_git_diff",
        lambda path, diff, check_only=False: {"ok": apply_ok, "stderr": "" if apply_ok else "corrupt patch"},
    )

    resume_spy = MagicMock()
    monkeypatch.setattr(runner, "resume_task_background", resume_spy)
    monkeypatch.setattr(runner, "register_task_queue", MagicMock())

    return TestClient(_app.app), store, resume_spy


def test_d17_implicit_apply_failure_blocks_accept(monkeypatch):
    """隐式 apply（apply_diff=false + 非 sandbox_first）失败 → 422、不 resume、回滚认领状态。"""
    client, store, resume_spy = _approve_client(monkeypatch, apply_ok=False)
    resp = client.post("/api/tasks/t-d17/approve", json={})
    assert resp.status_code == 422, f"隐式 apply 失败必须阻断 accept，实际 {resp.status_code}: {resp.text}"
    resume_spy.assert_not_called()
    # 回滚认领：status 恢复原审核态（任务留在可重试/待人工状态）
    rollback_calls = [c for c in store.update_task.call_args_list
                      if c.kwargs.get("status") == "DELIVERING" or ("DELIVERING" in c.args)]
    assert rollback_calls, "apply 失败后必须回滚认领状态到原审核态"


def test_d17_explicit_apply_failure_still_blocks(monkeypatch):
    """显式 apply_diff=true 失败 → 既有 422 语义不回归。"""
    client, store, resume_spy = _approve_client(monkeypatch, apply_ok=False)
    resp = client.post("/api/tasks/t-d17/approve", json={"apply_diff": True})
    assert resp.status_code == 422
    resume_spy.assert_not_called()


def test_d17_apply_success_resumes(monkeypatch):
    """apply 成功 → 照常 resume accept（治本不误伤正常路径）。"""
    client, store, resume_spy = _approve_client(monkeypatch, apply_ok=True)
    resp = client.post("/api/tasks/t-d17/approve", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("apply_diff", {}).get("ok") is True
    resume_spy.assert_called_once()


# ══════════════════════════════════════════════════════════════════
# D18 — cancel 终止流 + 终态载荷并入 complete
# ══════════════════════════════════════════════════════════════════

def _cancelled_topic():
    """预置一个已发布 cancelled + 哨兵事件的 fanout 主题。

    哨兵（cancelled 之后的 complete）用于区分治本前后行为且两态都不挂起：
    治本前 cancelled 被当 progress、流继续 → 哨兵可见；治本后 cancelled 即 break → 哨兵不可见。
    """
    import swarm.brain.runner as runner

    topic = runner._FanoutTopic()
    topic.publish({"step": "cancelled", "status": "cancelled", "message": "任务已取消", "progress": -1})
    topic.publish({"step": "complete", "status": "done", "message": "SENTINEL_AFTER_CANCEL", "progress": 100})
    return topic


def test_d18_sse_terminates_on_cancelled(monkeypatch):
    import importlib
    _app = importlib.import_module("swarm.api.app")  # api/__init__ 遮蔽 app 子模块，须 importlib
    import swarm.brain.runner as runner
    from fastapi.testclient import TestClient

    store = MagicMock()
    store.get_task.return_value = {"id": "t-d18", "project_id": "p1", "status": "CANCELLED"}
    monkeypatch.setattr(_app, "store", store)

    topic = _cancelled_topic()
    monkeypatch.setattr(runner, "subscribe_task", lambda tid: (topic, topic.subscribe()))

    client = TestClient(_app.app)
    resp = client.get("/api/tasks/t-d18/stream")
    assert resp.status_code == 200
    assert "任务已取消" in resp.text
    assert "SENTINEL_AFTER_CANCEL" not in resp.text, \
        "SSE 收到 cancelled 后必须立即终止流（不再继续消费后续事件/永久挂起）"


def test_d18_ws_terminates_on_cancelled(monkeypatch):
    import importlib
    _app = importlib.import_module("swarm.api.app")  # api/__init__ 遮蔽 app 子模块，须 importlib
    import swarm.api.auth as api_auth
    import swarm.brain.runner as runner
    from fastapi.testclient import TestClient
    from swarm.auth.store import SwarmUser

    store = MagicMock()
    store.get_task.return_value = {"id": "t-d18w", "project_id": "p1", "status": "CANCELLED"}
    monkeypatch.setattr(_app, "store", store)

    admin = SwarmUser(id="u-adm", username="a", display_name=None,
                      global_role="admin", must_change_password=False)
    monkeypatch.setattr(api_auth, "authenticate_ws", lambda ws: admin)

    topic = _cancelled_topic()
    monkeypatch.setattr(runner, "subscribe_task", lambda tid: (topic, topic.subscribe()))

    client = TestClient(_app.app)
    steps: list[str] = []
    with client.websocket_connect("/ws/tasks/t-d18w") as ws:
        for _ in range(3):  # 有界收取：治本后仅 1 条即断开
            try:
                msg = ws.receive_json()
            except Exception:
                break
            steps.append((msg.get("data") or {}).get("step", ""))
            if (msg.get("data") or {}).get("message") == "SENTINEL_AFTER_CANCEL":
                break
    assert "cancelled" in steps, f"WS 必须把 cancelled 事件送达订阅端: {steps}"
    assert all((s != "complete") for s in steps), \
        f"WS 收到 cancelled 后必须终止（哨兵不可达）: {steps}"


def test_d18_complete_event_carries_result_payload(monkeypatch):
    """正常终态：complete 事件并入 result 载荷（merged_diff 可达订阅端），不再发独立 result 事件。"""
    import swarm.brain.runner as runner

    store = MagicMock()
    store.get_task.return_value = {"id": "t-d18r", "project_id": "p1", "description": "x"}
    store.estimate_token_usage.return_value = {}
    store.compute_task_duration_seconds.return_value = 1.0
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_sync_task_from_state", lambda tid, st: None)

    topic = runner._FanoutTopic()
    sub = topic.subscribe()
    state = {"task_description": "x", "merged_diff": "diff --git a/f b/f\n+x\n", "l2_passed": True}
    asyncio.run(runner._handle_post_run("t-d18r", state, topic))

    events = []
    while not sub.empty():
        events.append(sub.get_nowait())
    completes = [e for e in events if e.get("step") == "complete"]
    results = [e for e in events if e.get("step") == "result"]
    assert len(completes) == 1, f"应恰好一个 complete 终态事件: {events}"
    assert not results, "独立 step:result 事件是死协议（订阅端 complete 即 break 永远收不到），必须删除"
    payload = completes[0].get("result") or {}
    assert payload.get("merged_diff"), "终态载荷（merged_diff）必须并入 complete 事件送达订阅端"


def test_d18_governor_partial_complete_carries_result(monkeypatch):
    """资源护栏 PARTIAL 抢救路径：complete 同样携带 result 载荷、无独立 result 事件。"""
    import swarm.brain.runner as runner

    store = MagicMock()
    store.get_task.return_value = {"id": "t-d18g", "project_id": "p1", "description": "x"}
    store.estimate_token_usage.return_value = {}
    store.compute_task_duration_seconds.return_value = 2.0
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_sync_task_from_state", lambda tid, st: None)
    monkeypatch.setattr(runner, "audit", lambda *a, **k: None)

    topic = runner._FanoutTopic()
    sub = topic.subscribe()
    state = {
        "task_description": "x",
        "merged_diff": "diff --git a/g b/g\n+y\n",
        "subtask_results": {"st-1": {"l1_passed": True}},
    }
    status = asyncio.run(runner._finalize_governor_partial(
        "t-d18g", state, topic, reason_code="token_budget", reason_msg="预算中止"))
    assert status == "PARTIAL"

    events = []
    while not sub.empty():
        events.append(sub.get_nowait())
    completes = [e for e in events if e.get("step") == "complete"]
    assert len(completes) == 1 and (completes[0].get("result") or {}).get("merged_diff")
    assert not [e for e in events if e.get("step") == "result"]


# ══════════════════════════════════════════════════════════════════
# D22 — 创建链路原子性
# ══════════════════════════════════════════════════════════════════

@requires_pg
def test_d22_store_create_task_single_insert_full_meta():
    """create_task 单条 INSERT 落全部初始 meta —— 两步窗口（SUBMITTED 残留未入队）不复存在。"""
    from swarm.project.store import create_project, create_task, ensure_tables, get_task

    ensure_tables()
    pid = f"_test_d22_p_{uuid.uuid4().hex[:8]}"
    tid = f"_test_d22_t_{uuid.uuid4().hex[:8]}"
    path = f"/tmp/_test_d22_{uuid.uuid4().hex[:8]}"
    try:
        create_project(pid, "_test_d22", path)
        row = create_task(
            tid, pid, "d22 atomic create",
            status="SUBMITTED", thread_id=tid,
            auto_accept=True, queue_priority="urgent",
        )
        assert row["status"] == "SUBMITTED"
        # INSERT 即完整（无需第二条 UPDATE）：进程在此后任意点崩溃都不会留下缺 meta 的残行
        persisted = get_task(tid)
        assert persisted["thread_id"] == tid
        assert persisted["auto_accept"] is True
        assert persisted["queue_priority"] == "urgent"
    finally:
        with psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True) as conn:
            with conn.cursor() as cur:
                for sql, arg in (
                    ("DELETE FROM task_audit_log WHERE task_id = %s", tid),
                    ("DELETE FROM task_records WHERE id = %s", tid),
                    ("DELETE FROM swarm_project_members WHERE project_id = %s", pid),
                    ("DELETE FROM preprocess_progress WHERE project_id = %s", pid),
                    ("DELETE FROM projects WHERE id = %s", pid),
                ):
                    try:
                        cur.execute(sql, (arg,))
                    except Exception:
                        pass


def _task_create_client(monkeypatch):
    import importlib
    _app = importlib.import_module("swarm.api.app")  # api/__init__ 遮蔽 app 子模块，须 importlib
    import swarm.brain.scheduler as scheduler
    import swarm.knowledge.readiness as readiness
    from fastapi.testclient import TestClient

    store = MagicMock()
    store.get_project.return_value = {"id": "p1", "path": "/tmp/_test_d22"}
    store.get_progress.return_value = {}
    store.find_active_duplicate_task.return_value = None
    created = {"id": "task-x", "status": "SUBMITTED"}
    store.create_task.return_value = created
    store.get_task.return_value = created
    store.create_notification.return_value = None
    monkeypatch.setattr(_app, "store", store)
    monkeypatch.setattr(readiness, "brain_task_ready", lambda proj, prog: (True, ""))

    submit_spy = MagicMock()
    monkeypatch.setattr(scheduler, "submit_task", submit_spy)
    return TestClient(_app.app), store, submit_spy


def test_d22_router_create_task_atomic_no_second_update(monkeypatch):
    """路由创建：初始 meta 全部经 create_task 单次写入，不再有第二条 UPDATE（部分写入窗口消灭）。"""
    client, store, submit_spy = _task_create_client(monkeypatch)
    resp = client.post("/api/projects/p1/tasks",
                       json={"description": "do x", "auto_accept": True, "priority": "urgent"})
    assert resp.status_code == 200, resp.text
    kwargs = store.create_task.call_args.kwargs
    assert kwargs.get("status") == "SUBMITTED"
    assert kwargs.get("thread_id") == kwargs.get("task_id"), "thread_id 必须随 INSERT 一并落库"
    assert kwargs.get("auto_accept") is True
    assert kwargs.get("queue_priority") == "urgent"
    store.update_task.assert_not_called()
    submit_spy.assert_called_once()


def test_d22_router_create_task_pooled_status(monkeypatch):
    """需求池模式：单次 INSERT 即 POOLED，不入调度队列。"""
    client, store, submit_spy = _task_create_client(monkeypatch)
    store.create_task.return_value = {"id": "task-p", "status": "POOLED"}
    store.get_task.return_value = {"id": "task-p", "status": "POOLED"}
    resp = client.post("/api/projects/p1/tasks", json={"description": "pool it", "pooled": True})
    assert resp.status_code == 200, resp.text
    assert store.create_task.call_args.kwargs.get("status") == "POOLED"
    store.update_task.assert_not_called()
    submit_spy.assert_not_called()


def _project_create_client(monkeypatch, tmp_path, *, member_fails: bool, compensate_fails: bool = False):
    import importlib
    _app = importlib.import_module("swarm.api.app")  # api/__init__ 遮蔽 app 子模块，须 importlib
    import swarm.api.deps as deps
    import swarm.auth.store as auth_store
    from fastapi.testclient import TestClient
    from swarm.auth.store import SwarmUser

    dev = SwarmUser(id="u-dev", username="dev1", display_name=None,
                    global_role="developer", must_change_password=False)
    monkeypatch.setattr(deps, "get_current_user", lambda request: dev)
    monkeypatch.setattr(auth_store, "user_can_on_project", lambda user, perm, pid=None: True)

    store = MagicMock()
    created = {"id": "proj-new", "path": str(tmp_path)}
    store.create_project.return_value = created
    if compensate_fails:
        store.delete_project.side_effect = RuntimeError("db down during compensation")
    else:
        store.delete_project.return_value = True
    store.claim_preprocess_slot.return_value = False  # 不 spawn 预处理
    monkeypatch.setattr(_app, "store", store)

    if member_fails:
        monkeypatch.setattr(auth_store, "set_project_member",
                            MagicMock(side_effect=RuntimeError("member insert failed")))
    else:
        monkeypatch.setattr(auth_store, "set_project_member", MagicMock())

    logger_spy = MagicMock()
    monkeypatch.setattr(_app, "logger", logger_spy)
    return TestClient(_app.app), store, logger_spy


def test_d22_project_member_failure_compensates_delete(monkeypatch, tmp_path):
    """set_project_member 失败 → 补偿删除刚建项目（不留创建者自己都看不到的孤儿），返回 5xx。"""
    client, store, _ = _project_create_client(monkeypatch, tmp_path, member_fails=True)
    resp = client.post("/api/projects", json={"name": "_test_d22", "path": str(tmp_path)})
    assert resp.status_code >= 500, f"成员写入失败必须报错，实际 {resp.status_code}: {resp.text}"
    store.delete_project.assert_called_once_with("proj-new")


def test_d22_project_compensation_failure_is_observable(monkeypatch, tmp_path):
    """补偿删除自身失败 → 仍报错且 error 日志留痕（孤儿可被运维发现），fail-closed 不吞。"""
    client, store, logger_spy = _project_create_client(
        monkeypatch, tmp_path, member_fails=True, compensate_fails=True)
    resp = client.post("/api/projects", json={"name": "_test_d22", "path": str(tmp_path)})
    assert resp.status_code >= 500
    assert logger_spy.error.called or logger_spy.exception.called, \
        "补偿删除失败必须 error 级日志留痕（孤儿项目 id 可追溯）"


def test_d22_project_member_success_no_compensation(monkeypatch, tmp_path):
    """正常路径：成员写入成功 → 不触发补偿删除（不误伤）。"""
    client, store, _ = _project_create_client(monkeypatch, tmp_path, member_fails=False)
    resp = client.post("/api/projects", json={"name": "_test_d22", "path": str(tmp_path)})
    assert resp.status_code == 200, resp.text
    store.delete_project.assert_not_called()
