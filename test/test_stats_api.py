#!/usr/bin/env python3
"""Phase 5 — /api/stats 与 /api/notifications 端点测试（mock store）"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

MOCK_STATS = {
    "total_tasks": 12,
    "completed": 8,
    "failed": 2,
    "cancelled": 1,
    "approved": 7,
    "accept_rate": 0.875,
    "avg_duration_seconds": 142.5,
    "total_tokens": 48000,
    "avg_tokens": 4000.0,
    "learning_effectiveness": {
        "recent_mistakes": 2,
        "prior_mistakes": 5,
        "trend": "improving",
    },
    "recent_tasks": [
        {
            "id": "task-1",
            "project_id": "p1",
            "description": "Fix login bug",
            "status": "DONE",
            "human_decision": "ACCEPT",
            "created_at": "2026-06-01T10:00:00+00:00",
            "updated_at": "2026-06-01T10:02:22+00:00",
            "duration_seconds": 142.0,
            "token_usage": {"input_tokens": 500, "output_tokens": 3500, "total": 4000, "estimate": True},
        }
    ],
}

MOCK_NOTIFICATIONS = [
    {
        "task_id": "task-1",
        "project_id": "p1",
        "description": "Fix login bug",
        "status": "DONE",
        "human_decision": "ACCEPT",
        "event_type": "task_completed",
        "updated_at": "2026-06-01T10:02:22+00:00",
        "message": "任务已完成: Fix login bug",
    }
]

EXPECTED_STATS_KEYS = {
    "total_tasks",
    "completed",
    "failed",
    "cancelled",
    "accept_rate",
    "avg_duration_seconds",
    "total_tokens",
    "avg_tokens",
    "recent_tasks",
}


def test_stats_endpoint_returns_expected_keys():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task_stats.return_value = MOCK_STATS
        client = TestClient(app)
        resp = client.get("/api/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert EXPECTED_STATS_KEYS.issubset(data.keys())
        assert data["total_tasks"] == 12
        assert data["accept_rate"] == 0.875
        assert data["total_tokens"] == 48000
        assert data["avg_tokens"] == 4000.0
        assert len(data["recent_tasks"]) == 1
        assert data["recent_tasks"][0]["token_usage"]["total"] == 4000
        mock_store.get_task_stats.assert_called_once_with(None)
    print("  ✅ GET /api/stats returns expected keys")


def test_stats_scoped_to_project():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    project = {"id": "p1", "name": "Demo"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = project
        mock_store.get_task_stats.return_value = {**MOCK_STATS, "project_id": "p1"}
        client = TestClient(app)
        resp = client.get("/api/stats?project_id=p1")
        assert resp.status_code == 200, resp.text
        mock_store.get_task_stats.assert_called_once_with("p1")

        resp2 = client.get("/api/projects/p1/stats")
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["project_id"] == "p1"
        assert resp2.json()["learning_effectiveness"]["trend"] == "improving"
    print("  ✅ GET /api/stats?project_id= and /api/projects/{id}/stats")


def test_stats_unknown_project_404():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = None
        client = TestClient(app)
        resp = client.get("/api/projects/missing/stats")
        assert resp.status_code == 404
    print("  ✅ GET /api/projects/{id}/stats 404 for missing project")


def test_notifications_endpoint():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.list_notifications.return_value = MOCK_NOTIFICATIONS
        mock_store.count_unread_notifications.return_value = 1
        client = TestClient(app)
        resp = client.get("/api/notifications")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "notifications" in data
        assert len(data["notifications"]) == 1
        assert data["notifications"][0]["event_type"] == "task_completed"
        assert data["unread_count"] == 1
        mock_store.list_notifications.assert_called_once()
    print("  ✅ GET /api/notifications")


def test_notifications_unread_count():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.count_unread_notifications.return_value = 3
        client = TestClient(app)
        resp = client.get("/api/notifications/unread_count")
        assert resp.status_code == 200, resp.text
        assert resp.json()["unread_count"] == 3
    print("  ✅ GET /api/notifications/unread_count")


def test_notifications_archive():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.archive_notification.return_value = True
        mock_store.archive_all_notifications.return_value = 5
        client = TestClient(app)
        resp = client.post("/api/notifications/42/archive")
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived"] is True
        mock_store.archive_notification.assert_called_once_with(42)

        resp = client.post("/api/notifications/archive_all")
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived_count"] == 5
    print("  ✅ POST /api/notifications/{id}/archive + archive_all")


if __name__ == "__main__":
    test_stats_endpoint_returns_expected_keys()
    test_stats_scoped_to_project()
    test_stats_unknown_project_404()
    test_notifications_endpoint()
    test_notifications_unread_count()
    test_notifications_archive()
    print("\nAll Phase 5 stats API tests passed.")
