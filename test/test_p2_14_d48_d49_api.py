"""P2-14 深读登记册 D48 / D49 行为测试。

D48：鉴权/重认证 PG 查询卸线程（_require_perm_async / _require_user_async / SSE 重认证间隔）。
D49：任务列表轻量列+分页；sandbox_status 创建者批量查询（消 N+1）。
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

import importlib.util
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ─── D48：鉴权卸线程收口 ───────────────────────────────


def test_d48_require_perm_async_runs_off_event_loop(monkeypatch):
    import swarm.api._shared as shared

    info: dict = {}

    def fake_perm(request, permission, project_id=None):
        info["thread"] = threading.get_ident()
        info["args"] = (permission, project_id)
        return "USER-SENTINEL"

    monkeypatch.setattr(shared, "_require_perm", fake_perm)

    async def main():
        loop_thread = threading.get_ident()
        res = await shared._require_perm_async(None, "task:read", "p1")
        return res, loop_thread

    res, loop_thread = asyncio.run(main())
    assert res == "USER-SENTINEL"
    assert info["args"] == ("task:read", "p1")
    assert info["thread"] != loop_thread  # 同步 PG 查询不再跑在事件循环线程


def test_d48_require_user_async_propagates_http_exception(monkeypatch):
    from fastapi import HTTPException

    import swarm.api._shared as shared

    def deny(request):
        raise HTTPException(status_code=401, detail="nope")

    monkeypatch.setattr(shared, "_require_user", deny)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(shared._require_user_async(None))
    assert ei.value.status_code == 401


def test_d48_sse_reauth_interval_env(monkeypatch):
    from swarm.api.routers.task import _sse_reauth_interval_s

    monkeypatch.delenv("SWARM_SSE_REAUTH_INTERVAL_S", raising=False)
    assert _sse_reauth_interval_s() == 30.0          # 默认与 stream_task 既有心跳节奏一致
    monkeypatch.setenv("SWARM_SSE_REAUTH_INTERVAL_S", "120")
    assert _sse_reauth_interval_s() == 120.0         # env 可配降频
    monkeypatch.setenv("SWARM_SSE_REAUTH_INTERVAL_S", "1")
    assert _sse_reauth_interval_s() == 5.0           # 下限钳 5s 防误配打爆 DB
    monkeypatch.setenv("SWARM_SSE_REAUTH_INTERVAL_S", "not-a-number")
    assert _sse_reauth_interval_s() == 30.0          # 非法回退默认


# ─── D49：任务列表轻量列 + 分页 ─────────────────────────


def _light_row():
    # 与 _TASK_SELECT_LIGHT 同序：id, project_id, description, status, complexity,
    # subtask_count, completed_subtasks, human_decision, thread_id, duration_seconds,
    # created_by_user_id, created_at, updated_at, uploaded_files, auto_confirm_vision,
    # pooled, abandoned_subtasks, auto_accept, queue_priority, base_commit
    return (
        "t1", "p1", "desc", "DONE", "medium",
        10, 7, "accept", "th-1", 12.5,
        "u1", "2026-07-07", "2026-07-07", '["f.png"]', False,
        False, 1, True, "normal", "abc123",
    )


def test_d49_row_to_task_light_shape():
    from swarm.project.store import _row_to_task, _row_to_task_light

    d = _row_to_task_light(_light_row())
    assert d["id"] == "t1" and d["status"] == "DONE" and d["complexity"] == "medium"
    assert d["remaining_subtasks"] == 10 - 7 - 1
    assert d["uploaded_files"] == ["f.png"]
    # 重字段不在轻量视图（这正是 D49 的点）
    for heavy in ("merged_diff", "plan", "l3_result", "token_usage", "merge_conflicts"):
        assert heavy not in d
    # 键名与全量视图同名同义（轻量键 ⊆ 全量键）
    full_keys = set(_row_to_task(tuple([None] * 26)).keys())
    assert set(d.keys()) <= full_keys


def test_d49_list_tasks_endpoint_uses_light_store_and_pagination():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = {"id": "p1"}
        mock_store.list_tasks_light.return_value = [
            {"id": "t1", "status": "DONE", "description": "d", "complexity": "simple"}
        ]
        client = TestClient(app)

        r = client.get("/api/projects/p1/tasks")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["tasks"][0]["id"] == "t1"
        # 缺省分页 = 默认上限（500），offset 0
        mock_store.list_tasks_light.assert_called_with("p1", limit=500, offset=0)

        r = client.get("/api/projects/p1/tasks?limit=10&offset=5")
        assert r.status_code == 200
        mock_store.list_tasks_light.assert_called_with("p1", limit=10, offset=5)
        # 全程未走全量重列查询（merged_diff/plan 不再每次轮询搬运）
        assert not mock_store.list_tasks.called


def test_d49_get_task_creators_empty_short_circuit():
    from swarm.project.store import get_task_creators

    assert get_task_creators([]) == {}   # 空集不打 DB
    assert get_task_creators([None, ""]) == {}


def test_d49_sandbox_status_batches_creator_lookup(monkeypatch):
    """非 admin 成员视角：创建者经一次批量查询获得，不再每沙箱一条 get_task（N+1）。"""
    from fastapi.testclient import TestClient

    import swarm.api._shared as shared
    import swarm.auth.store as auth_store
    import swarm.project.store as pstore
    from swarm.api.app import app

    class _User:
        id = "u1"
        global_role = "developer"
        must_change_password = False

    monkeypatch.setattr(shared, "_require_user", lambda request: _User())
    monkeypatch.setattr(auth_store, "get_project_member_role", lambda pid, uid: "developer")

    batch_calls: list = []

    def fake_creators(tids, conn_str=None):
        batch_calls.append(sorted(tids))
        return {"t-mine": "u1", "t-other": "u2"}

    monkeypatch.setattr(pstore, "get_task_creators", fake_creators)
    get_task_spy = MagicMock(side_effect=AssertionError("不应再逐沙箱 get_task"))
    monkeypatch.setattr(pstore, "get_task", get_task_spy)

    server_list = [
        {"id": "sb1", "status": "running"},
        {"id": "sb2", "status": "running"},
    ]
    meta = {
        "sb1": {"project_id": "p1", "task_id": "t-mine", "source": "task"},
        "sb2": {"project_id": "p1", "task_id": "t-other", "source": "task"},
    }
    manager = MagicMock()
    manager.active_ids = []
    manager.get_sandbox_meta.side_effect = lambda sid: meta.get(sid)
    manager.sandboxes_for_project.return_value = {"sb1", "sb2"}

    with patch("swarm.api.app._fetch_sandbox_list_from_server", return_value=server_list), \
         patch("swarm.api.app._get_sandbox_manager", return_value=manager):
        client = TestClient(app)
        r = client.get("/api/sandbox/status")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {sb["id"] for sb in body["sandboxes"]}
    assert ids == {"sb1"}                      # 成员仅见自建任务沙箱（行为不变）
    assert batch_calls == [["t-mine", "t-other"]]  # 一次批量查询
    assert not get_task_spy.called                 # N+1 消除
    # D47b：非 admin 不见内部基建坐标（行为回归护栏）
    assert "api_url" not in body["config"]


def test_d49_sandbox_status_batch_failure_falls_back_per_task(monkeypatch):
    """批量查询失败 → fail-closed 回退旧逐沙箱路径，可见性结论不变。"""
    from fastapi.testclient import TestClient

    import swarm.api._shared as shared
    import swarm.auth.store as auth_store
    import swarm.project.store as pstore
    from swarm.api.app import app

    class _User:
        id = "u1"
        global_role = "developer"
        must_change_password = False

    monkeypatch.setattr(shared, "_require_user", lambda request: _User())
    monkeypatch.setattr(auth_store, "get_project_member_role", lambda pid, uid: "developer")

    def broken_batch(tids, conn_str=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(pstore, "get_task_creators", broken_batch)
    monkeypatch.setattr(
        pstore, "get_task",
        lambda tid: {"created_by_user_id": "u1"} if tid == "t-mine" else {"created_by_user_id": "u2"},
    )

    server_list = [{"id": "sb1", "status": "running"}, {"id": "sb2", "status": "running"}]
    meta = {
        "sb1": {"project_id": "p1", "task_id": "t-mine", "source": "task"},
        "sb2": {"project_id": "p1", "task_id": "t-other", "source": "task"},
    }
    manager = MagicMock()
    manager.active_ids = []
    manager.get_sandbox_meta.side_effect = lambda sid: meta.get(sid)
    manager.sandboxes_for_project.return_value = {"sb1", "sb2"}

    with patch("swarm.api.app._fetch_sandbox_list_from_server", return_value=server_list), \
         patch("swarm.api.app._get_sandbox_manager", return_value=manager):
        client = TestClient(app)
        r = client.get("/api/sandbox/status")
    assert r.status_code == 200, r.text
    assert {sb["id"] for sb in r.json()["sandboxes"]} == {"sb1"}
