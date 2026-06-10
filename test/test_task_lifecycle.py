#!/usr/bin/env python3
"""任务 cancel / retry / 创建就绪门控 API 测试（mock store + runner）"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_cancel_running_task():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {"id": "task-1", "project_id": "p1", "status": "DISPATCHING"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        with patch("swarm.brain.runner.is_task_running", return_value=True):
            with patch("swarm.brain.runner.is_task_orphaned", return_value=False):
                with patch("swarm.brain.runner.cancel_task", new=AsyncMock(return_value=True)):
                    client = TestClient(app)
                    resp = client.post("/api/tasks/task-1/cancel")
                    assert resp.status_code == 200, resp.text
                    assert "取消" in resp.json().get("message", "")
    print("  ✅ POST /cancel running task")


def test_cancel_not_cancellable():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {"id": "task-1", "status": "DELIVERING"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        with patch("swarm.brain.runner.is_task_running", return_value=False):
            with patch("swarm.brain.runner.is_task_orphaned", return_value=False):
                client = TestClient(app)
                resp = client.post("/api/tasks/task-1/cancel")
                assert resp.status_code == 409
    print("  ✅ POST /cancel 409 when not cancellable")


def test_retry_failed_task():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {"id": "task-1", "project_id": "p1", "status": "FAILED"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        with patch("swarm.brain.runner.can_retry_task", return_value=(True, "")):
            with patch("swarm.brain.runner.retry_task_background") as mock_retry:
                with patch("swarm.brain.runner.register_task_queue"):
                    client = TestClient(app)
                    resp = client.post(
                        "/api/tasks/task-1/retry",
                        json={"auto_accept": True},
                    )
                    assert resp.status_code == 200, resp.text
                    mock_retry.assert_called_once_with("task-1", auto_accept=True)
    print("  ✅ POST /retry failed task")


def test_retry_not_allowed():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = {"id": "t1", "status": "DISPATCHING"}
        with patch("swarm.brain.runner.can_retry_task", return_value=(False, "任务仍在执行中")):
            client = TestClient(app)
            resp = client.post("/api/tasks/t1/retry")
            assert resp.status_code == 409
    print("  ✅ POST /retry 409 when not allowed")


def test_create_task_rejects_unpreprocessed():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    project = {"id": "p1", "status": "NEW", "graph_status": "NONE"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = project
        mock_store.get_progress.return_value = None
        client = TestClient(app)
        resp = client.post(
            "/api/projects/p1/tasks",
            json={"description": "add feature"},
        )
        assert resp.status_code == 409, resp.text
        assert "预处理" in resp.json().get("detail", "")
    print("  ✅ POST /tasks 409 when not ready")


def test_create_task_allows_ready_project():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    project = {"id": "p1", "status": "READY", "graph_status": "INDEXED"}
    created = {"id": "new-task", "project_id": "p1", "description": "x", "status": "SUBMITTED"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = project
        mock_store.get_progress.return_value = {
            "phase": "complete",
            "index_stats": {},
            "embed_stats": {},
        }
        mock_store.create_task.return_value = created
        mock_store.get_task.return_value = created
        with patch("swarm.brain.runner.start_task_background"):
            client = TestClient(app)
            resp = client.post(
                "/api/projects/p1/tasks",
                json={"description": "add feature"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["task"]["id"] == "new-task"
    print("  ✅ POST /tasks ok when ready")


def test_create_task_allows_partial_ready():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    project = {"id": "p1", "status": "READY", "graph_status": "INDEXED"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = project
        mock_store.get_progress.return_value = {
            "phase": "complete",
            "index_stats": {"skipped": True},
            "embed_stats": {},
        }
        mock_store.create_task.return_value = {"id": "t2", "project_id": "p1"}
        mock_store.get_task.return_value = {"id": "t2", "project_id": "p1"}
        with patch("swarm.brain.runner.start_task_background"):
            client = TestClient(app)
            resp = client.post("/api/projects/p1/tasks", json={"description": "x"})
            assert resp.status_code == 200, resp.text
    print("  ✅ POST /tasks ok when partial ready")


def test_readiness_assessment():
    from swarm.knowledge.readiness import assess_knowledge_readiness, brain_task_ready

    ok, _ = brain_task_ready(
        {"status": "READY"},
        {"phase": "complete", "index_stats": {}, "embed_stats": {}},
    )
    assert ok

    ok2, msg = brain_task_ready({"status": "NEW"}, None)
    assert not ok2
    assert "预处理" in msg

    r = assess_knowledge_readiness(
        {"status": "PREPROCESSING"},
        {"phase": "indexing"},
    )
    assert r["level"] == "running"
    print("  ✅ knowledge readiness helpers")


def main() -> int:
    tests = [
        test_cancel_running_task,
        test_cancel_not_cancellable,
        test_retry_failed_task,
        test_retry_not_allowed,
        test_create_task_rejects_unpreprocessed,
        test_create_task_allows_ready_project,
        test_create_task_allows_partial_ready,
        test_readiness_assessment,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\n✅ 全部 {len(tests)} 项 task lifecycle 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
