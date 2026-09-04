"""Batch 2-B：执行面 readiness 与 API 准入行为锁（全离线）。"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


async def _dep_ok():
    return True, "ok"


def _client():
    from swarm.api.app import app

    return TestClient(app)


@contextmanager
def _lifespan_active(value: bool):
    from swarm.api.app import app

    missing = object()
    old = getattr(app.state, "lifespan_active", missing)
    app.state.lifespan_active = value
    try:
        yield
    finally:
        if old is missing:
            delattr(app.state, "lifespan_active")
        else:
            app.state.lifespan_active = old


def test_ready_reports_dead_execution_plane_as_machine_readable_503():
    """PG/Redis/Qdrant 都活着但 scheduler 死亡时，readiness 不能继续假绿。"""
    with patch("swarm.api.app._probe_pg_ready", _dep_ok), \
         patch("swarm.api.app._probe_qdrant_ready", _dep_ok), \
         patch("swarm.api.app.redis_enabled", return_value=False), \
         patch(
             "swarm.api.app._probe_execution_plane_ready",
             AsyncMock(return_value=(False, "local_scheduler_stopped")),
         ):
        response = _client().get("/api/health/ready")

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["execution_plane"] == {
        "ok": False,
        "detail": "local_scheduler_stopped",
    }


@pytest.mark.asyncio
async def test_follower_is_explicitly_not_ready_even_when_remote_leader_exists(monkeypatch):
    """runner/SSE 均为本地态；standby 不能借远端 lease 假装可执行。"""
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler
    import swarm.infra.scheduler_leadership as leadership

    class FollowerBackend:
        async def is_held(self, key: str) -> bool:
            return False

    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: False)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    monkeypatch.setattr(leadership, "get_coordination_backend", lambda: FollowerBackend())

    assert await app_mod._probe_execution_plane_ready() == (False, "standby_follower")


@pytest.mark.asyncio
async def test_local_leader_with_dead_consumer_is_not_ready(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler
    import swarm.infra.scheduler_leadership as leadership

    class LocalLeaderBackend:
        async def is_held(self, key: str) -> bool:
            return True

    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: False)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    monkeypatch.setattr(leadership, "get_coordination_backend", lambda: LocalLeaderBackend())

    assert await app_mod._probe_execution_plane_ready() == (
        False,
        "local_leader_scheduler_stopped",
    )


@pytest.mark.asyncio
async def test_single_instance_requires_local_consumer(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler
    import swarm.infra.scheduler_leadership as leadership

    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: False)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    monkeypatch.setattr(leadership, "get_coordination_backend", lambda: None)
    assert await app_mod._probe_execution_plane_ready() == (
        False,
        "local_scheduler_stopped",
    )


@pytest.mark.asyncio
async def test_running_local_consumer_does_not_mask_lost_leader_lease(monkeypatch):
    """watchdog 尚未来得及停 consumer 的窗口，readiness 也必须按真实 lease fail-closed。"""
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler
    import swarm.infra.scheduler_leadership as leadership

    class LostBackend:
        async def verify_leadership(self, key: str) -> bool:
            return False

    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    monkeypatch.setattr(leadership, "get_coordination_backend", lambda: LostBackend())
    assert await app_mod._probe_execution_plane_ready() == (
        False,
        "local_scheduler_leadership_lost",
    )


@pytest.mark.asyncio
async def test_running_local_consumer_with_effective_lease_is_ready(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler
    import swarm.infra.scheduler_leadership as leadership

    class LeaderBackend:
        async def verify_leadership(self, key: str) -> bool:
            return True

    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    monkeypatch.setattr(leadership, "get_coordination_backend", lambda: LeaderBackend())
    assert await app_mod._probe_execution_plane_ready() == (
        True,
        "local_scheduler_running",
    )


@pytest.mark.asyncio
async def test_execution_admission_raises_machine_readable_503_in_active_lifespan(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    old = getattr(app_mod.app.state, "lifespan_active", None)
    app_mod.app.state.lifespan_active = True
    monkeypatch.setattr(
        app_mod,
        "_probe_execution_plane_ready",
        AsyncMock(return_value=(False, "leader_missing")),
    )
    try:
        with pytest.raises(HTTPException) as raised:
            await app_mod.require_execution_plane_ready()
    finally:
        if old is None:
            delattr(app_mod.app.state, "lifespan_active")
        else:
            app_mod.app.state.lifespan_active = old

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "code": "execution_plane_unavailable",
        "reason": "leader_missing",
    }


@pytest.mark.asyncio
async def test_execution_admission_rejects_when_lifespan_never_started(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    monkeypatch.delattr(app_mod.app.state, "lifespan_active", raising=False)
    monkeypatch.setattr(
        app_mod,
        "_probe_execution_plane_ready",
        AsyncMock(side_effect=AssertionError("lifespan-off must reject before probing")),
    )
    with pytest.raises(HTTPException) as raised:
        await app_mod.require_execution_plane_ready()
    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "code": "execution_plane_unavailable",
        "reason": "app_not_started",
    }


@pytest.mark.asyncio
async def test_stopping_execution_plane_is_never_ready(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler

    monkeypatch.setattr(scheduler, "is_stopping", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "is_consumer_running",
        lambda: (_ for _ in ()).throw(AssertionError("stopping wins")),
    )
    assert await app_mod._probe_execution_plane_ready() == (False, "scheduler_stopping")


@pytest.mark.parametrize(
    ("method", "url", "payload", "task_status"),
    [
        ("post", "/api/projects/p1/tasks", {"description": "x"}, None),
        ("post", "/api/tasks/t1/retry", {}, "FAILED"),
        ("post", "/api/tasks/t1/execute", {}, "POOLED"),
        ("post", "/api/tasks/t1/approve", {}, "DELIVERING"),
        ("post", "/api/tasks/t1/revise", {"feedback": "fix"}, "DELIVERING"),
        ("post", "/api/tasks/t1/reject", {}, "DELIVERING"),
        ("post", "/api/tasks/t1/clarify", {"action": "skip"}, "CLARIFYING"),
        (
            "post",
            "/api/tasks/t1/review-design",
            {"decision": "approve"},
            "DESIGN_REVIEW",
        ),
    ],
)
def test_every_execution_entrypoint_rejects_before_mutation(
    method, url, payload, task_status
):
    """stopping 真实 guard 覆盖 8 入口，且拒绝发生在状态写/认领前。"""
    import swarm.brain.scheduler as scheduler

    task = {
        "id": "t1",
        "project_id": "p1",
        "status": task_status,
        "description": "x",
        "merged_diff": "",
    }
    with _lifespan_active(True):
        with patch.object(scheduler, "is_stopping", return_value=True), \
             patch.object(
                 scheduler,
                 "is_consumer_running",
                 side_effect=AssertionError("stopping must reject before consumer probe"),
             ), \
             patch("swarm.api.app.store") as store, \
             patch("swarm.brain.runner.can_retry_task", return_value=(True, "")):
            store.get_task.return_value = task
            store.get_project.return_value = {
                "id": "p1",
                "status": "READY",
                "graph_status": "INDEXED",
            }
            store.get_progress.return_value = {
                "phase": "complete",
                "index_stats": {"symbols": 1},
                "embed_stats": {"vectors": 1},
            }
            store.find_active_duplicate_task.return_value = None
            response = getattr(_client(), method)(url, json=payload)

    assert response.status_code == 503, (url, response.text)
    assert response.json()["detail"]["code"] == "execution_plane_unavailable"
    assert response.json()["detail"]["reason"] == "scheduler_stopping"
    store.create_task.assert_not_called()
    store.update_task.assert_not_called()
    store.claim_human_gate.assert_not_called()


def test_standby_follower_real_guard_rejects_create_without_mutation():
    import swarm.brain.scheduler as scheduler
    import swarm.infra.scheduler_leadership as leadership

    class FollowerBackend:
        async def is_held(self, _key: str) -> bool:
            return False

    with _lifespan_active(True):
        with patch.object(scheduler, "is_stopping", return_value=False), \
             patch.object(scheduler, "is_consumer_running", return_value=False), \
             patch.object(
                 leadership, "get_coordination_backend", return_value=FollowerBackend()
             ), \
             patch("swarm.api.app.store") as store:
            store.get_project.return_value = {
                "id": "p1",
                "status": "READY",
                "graph_status": "INDEXED",
            }
            store.get_progress.return_value = {
                "phase": "complete",
                "index_stats": {"symbols": 1},
                "embed_stats": {"vectors": 1},
            }
            store.find_active_duplicate_task.return_value = None
            response = _client().post(
                "/api/projects/p1/tasks", json={"description": "must stay local"}
            )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["reason"] == "standby_follower"
    store.create_task.assert_not_called()


def test_app_not_started_real_guard_rejects_create_without_mutation():
    with _lifespan_active(False):
        with patch("swarm.api.app.store") as store:
            store.get_project.return_value = {
                "id": "p1",
                "status": "READY",
                "graph_status": "INDEXED",
            }
            store.get_progress.return_value = {
                "phase": "complete",
                "index_stats": {"symbols": 1},
                "embed_stats": {"vectors": 1},
            }
            store.find_active_duplicate_task.return_value = None
            response = _client().post(
                "/api/projects/p1/tasks", json={"description": "not started"}
            )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["reason"] == "app_not_started"
    store.create_task.assert_not_called()


def test_duplicate_create_returns_existing_task_even_when_execution_plane_is_down():
    """重复提交没有新增执行副作用，应优先返回既有任务而非伪装成 503。"""
    duplicate = {
        "id": "existing",
        "project_id": "p1",
        "status": "ANALYZING",
        "description": "same",
    }
    guard = AsyncMock(side_effect=AssertionError("duplicate must not need execution admission"))
    with patch("swarm.api.app.require_execution_plane_ready", guard), \
         patch("swarm.api.app.store") as store:
        store.get_project.return_value = {
            "id": "p1",
            "status": "READY",
            "graph_status": "INDEXED",
        }
        store.get_progress.return_value = {
            "phase": "complete",
            "index_stats": {"symbols": 1},
            "embed_stats": {"vectors": 1},
        }
        store.find_active_duplicate_task.return_value = duplicate
        response = _client().post(
            "/api/projects/p1/tasks", json={"description": "same"}
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "duplicate"
    store.create_task.assert_not_called()
    guard.assert_not_awaited()


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        ("/api/tasks/t1/approve", {}),
        ("/api/tasks/t1/revise", {"feedback": "again"}),
        ("/api/tasks/t1/reject", {}),
        ("/api/tasks/t1/clarify", {"action": "skip"}),
        ("/api/tasks/t1/review-design", {"decision": "approve"}),
    ],
)
def test_duplicate_human_decision_stays_idempotent_when_execution_plane_is_down(
    url, payload
):
    """已经离开人工闸态的重复点击没有执行动作，仍返回幂等成功。"""
    task = {"id": "t1", "project_id": "p1", "status": "ANALYZING", "merged_diff": ""}
    guard = AsyncMock(side_effect=AssertionError("duplicate decision must not need admission"))
    with patch("swarm.api.app.require_execution_plane_ready", guard), \
         patch("swarm.api.app.store") as store:
        store.get_task.return_value = task
        response = _client().post(url, json=payload)

    assert response.status_code == 200, (url, response.text)
    store.claim_human_gate.assert_not_called()
    guard.assert_not_awaited()


def test_pooled_create_remains_available_when_execution_plane_is_down():
    """只入需求池不触发执行，不应被执行面故障误杀；真正 execute 另有准入闸。"""
    project = {"id": "p1", "status": "READY", "graph_status": "INDEXED"}
    task = {"id": "t1", "project_id": "p1", "status": "POOLED"}
    guard = AsyncMock(
        side_effect=AssertionError("pooled create must not probe execution plane")
    )
    with patch("swarm.api.app.require_execution_plane_ready", guard), \
         patch("swarm.api.app.store") as store:
        store.get_project.return_value = project
        store.get_progress.return_value = {
            "phase": "complete",
            "index_stats": {"symbols": 1},
            "embed_stats": {"vectors": 1},
        }
        store.find_active_duplicate_task.return_value = None
        store.create_task.return_value = task
        store.get_task.return_value = task
        response = _client().post(
            "/api/projects/p1/tasks", json={"description": "later", "pooled": True}
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pooled"
    guard.assert_not_awaited()


def test_follower_cannot_cancel_remote_active_task_or_mutate_store():
    """standby 看不到 leader 的本地句柄，不能把远端活跃任务当 orphan 取消。"""
    import swarm.infra.scheduler_leadership as leadership

    class FollowerBackend:
        async def verify_leadership(self, _key: str) -> bool:
            return False

    task = {"id": "t1", "project_id": "p1", "status": "ANALYZING"}
    with patch.object(leadership, "get_coordination_backend", return_value=FollowerBackend()), \
         patch("swarm.api.app.store") as store, \
         patch("swarm.brain.runner.is_task_running", return_value=False), \
         patch("swarm.brain.runner.is_task_orphaned", return_value=True), \
         patch("swarm.brain.runner.cancel_task", new_callable=AsyncMock) as cancel:
        store.get_task.return_value = task
        response = _client().post("/api/tasks/t1/cancel")

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "execution_leader_required"
    cancel.assert_not_awaited()
    store.claim_human_gate.assert_not_called()
    store.update_task.assert_not_called()


def test_follower_cannot_force_delete_remote_active_task():
    """force 也不能跨副本越权：没有跨副本 cancel signal 时只能由 leader 终止。"""
    import swarm.infra.scheduler_leadership as leadership

    class FollowerBackend:
        async def verify_leadership(self, _key: str) -> bool:
            return False

    task = {"id": "t1", "project_id": "p1", "status": "DISPATCHING"}
    with patch.object(leadership, "get_coordination_backend", return_value=FollowerBackend()), \
         patch("swarm.api.app.store") as store, \
         patch("swarm.brain.runner.is_task_running", return_value=False), \
         patch("swarm.brain.runner.is_task_orphaned", return_value=True), \
         patch("swarm.brain.runner.cancel_task", new_callable=AsyncMock) as cancel:
        store.get_task.return_value = task
        response = _client().delete("/api/tasks/t1?force=true")

    assert response.status_code == 503, response.text
    cancel.assert_not_awaited()
    store.delete_task.assert_not_called()


def test_active_orphan_force_delete_cancels_before_deleting():
    """单实例丢句柄仍须走取消清沙箱/结算，而不是把活跃 DB 行直接删掉。"""
    task = {"id": "t1", "project_id": "p1", "status": "ANALYZING"}
    events: list[str] = []
    with patch("swarm.api.app.store") as store, \
         patch("swarm.brain.runner.is_task_running", return_value=False), \
         patch("swarm.brain.runner.is_task_orphaned", return_value=True), \
         patch(
             "swarm.brain.runner.cancel_task",
             new=AsyncMock(side_effect=lambda _tid: events.append("cancel") or True),
         ):
        store.get_task.return_value = task
        store.delete_task.side_effect = lambda _tid, **_kw: events.append("delete") or True
        response = _client().delete("/api/tasks/t1?force=true")

    assert response.status_code == 200, response.text
    assert events == ["cancel", "delete"]


def test_task_delete_cas_miss_returns_conflict_not_delete_new_epoch():
    """最终快照到 DELETE 之间发生 execute/retry 时，条件删除 miss 必须 409。"""
    initial = {
        "id": "t1", "project_id": "p1", "status": "FAILED", "updated_at": "old",
    }
    latest = {**initial, "updated_at": "snapshot"}
    with patch("swarm.api.app.store") as store:
        store.get_task.side_effect = [initial, latest]
        store.delete_task.return_value = False
        response = _client().delete("/api/tasks/t1")

    assert response.status_code == 409, response.text
    store.delete_task.assert_called_once_with(
        "t1", expected_status="FAILED", expected_updated_at="snapshot",
    )


def test_project_delete_fences_before_cancellation_and_requires_fence_on_delete():
    """项目删除先持久 DELETING，再清任务，最终 hard delete 只能消费该围栏。"""
    events: list[str] = []
    with patch("swarm.api.app.store") as store, patch(
        "swarm.brain.runner.cancel_project_tasks",
        new=AsyncMock(side_effect=lambda _pid: events.append("cancel") or 0),
    ):
        store.get_project.side_effect = [
            {"id": "p1", "status": "READY"},
            {"id": "p1", "status": "DELETING"},
        ]
        store.claim_project_deletion.side_effect = (
            lambda _pid: events.append("fence") or {"id": "p1", "status": "DELETING"}
        )
        store.list_tasks.return_value = []
        store.delete_project.side_effect = (
            lambda _pid, **_kw: events.append("delete") or True
        )
        response = _client().delete("/api/projects/p1")

    assert response.status_code == 200, response.text
    assert events == ["fence", "cancel", "delete"]
    store.delete_project.assert_called_once_with("p1", require_deleting=True)


def test_follower_cannot_delete_project_with_remote_active_tasks():
    """项目级联删除不得让 leader 上的 runner 失去 DB 行而成为幽灵。"""
    import swarm.infra.scheduler_leadership as leadership

    class FollowerBackend:
        async def verify_leadership(self, _key: str) -> bool:
            return False

    with patch.object(leadership, "get_coordination_backend", return_value=FollowerBackend()), \
         patch("swarm.api.app.store") as store, \
         patch("swarm.brain.runner.cancel_project_tasks", new_callable=AsyncMock) as cancel:
        store.get_project.return_value = {"id": "p1"}
        store.delete_project.return_value = False
        store.list_tasks.return_value = [
            {"id": "t1", "project_id": "p1", "status": "ANALYZING"}
        ]
        response = _client().delete("/api/projects/p1")

    assert response.status_code == 503, response.text
    cancel.assert_not_awaited()
    store.claim_project_deletion.assert_not_called()
    store.delete_project.assert_not_called()


def test_project_delete_stops_when_active_task_cannot_settle():
    """级联取消部分失败后，重新枚举仍有活跃任务就必须保留整个项目。"""
    active = {"id": "t1", "project_id": "p1", "status": "ANALYZING"}
    with patch("swarm.api.app.store") as store, \
         patch("swarm.brain.runner.cancel_project_tasks", new=AsyncMock(return_value=0)):
        store.get_project.return_value = {"id": "p1"}
        store.delete_project.return_value = False
        store.list_tasks.side_effect = [[active], [active], [active]]
        response = _client().delete("/api/projects/p1")

    assert response.status_code == 409, response.text
    store.delete_project.assert_not_called()


def test_task_apply_lock_recheck_rejects_deleted_task(monkeypatch):
    """拿到写锁后 task 已删/换 epoch 时不得写旧 diff。"""
    class Lock:
        def __init__(self, *_a):
            pass

        def acquire(self):
            return True

        def release(self):
            return None

    task = {
        "id": "t1", "project_id": "p1", "status": "DONE", "merged_diff": "patch",
        "thread_id": "epoch-1", "updated_at": "v1",
    }
    apply = MagicMock(return_value={"ok": True})
    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", Lock)
    monkeypatch.setattr("swarm.project.diff_apply.apply_git_diff", apply)
    with patch("swarm.api.app.store") as store:
        store.get_task.side_effect = [task, None]
        store.get_project.return_value = {"id": "p1", "path": "/tmp", "status": "READY"}
        response = _client().post("/api/tasks/t1/apply-diff", json={})

    assert response.status_code == 409, response.text
    apply.assert_not_called()


def test_worker_apply_lock_recheck_rejects_deleting_project(monkeypatch):
    """Worker apply 锁外读到 READY、锁后已 DELETING 时不得写盘。"""
    class Lock:
        def __init__(self, *_a):
            pass

        def acquire(self):
            return True

        def release(self):
            return None

    apply = MagicMock(return_value={"ok": True})
    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", Lock)
    monkeypatch.setattr("swarm.project.diff_apply.apply_git_diff", apply)
    with patch("swarm.api.app.store") as store:
        store.get_project.side_effect = [
            {"id": "p1", "path": "/tmp", "status": "READY"},
            {"id": "p1", "path": "/tmp", "status": "DELETING"},
        ]
        response = _client().post(
            "/api/projects/p1/apply-diff", json={"diff": "patch", "check_only": False}
        )

    assert response.status_code == 409, response.text
    apply.assert_not_called()
