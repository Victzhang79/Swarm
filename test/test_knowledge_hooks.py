#!/usr/bin/env python3
"""approve → schedule_incremental_update / KnowledgeUpdater 钩子测试"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_approve_schedules_incremental_update():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {
        "id": "task-1",
        "project_id": "proj-1",
        "merged_diff": "--- a/foo.py\n+++ b/foo.py\n",
        "status": "DELIVERING",
    }
    project = {"id": "proj-1", "path": "/tmp/p"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        mock_store.get_project.return_value = project
        mock_store.update_task.return_value = task
        with patch("swarm.brain.runner.resume_task_background"):
            with patch("swarm.brain.runner.register_task_queue"):
                with patch("swarm.knowledge.hooks.schedule_incremental_update") as mock_hook:
                    client = TestClient(app)
                    resp = client.post("/api/tasks/task-1/approve", json={})
                    assert resp.status_code == 200, resp.text
                    mock_hook.assert_called_once_with(
                        "proj-1",
                        "/tmp/p",
                        task["merged_diff"],
                        task_id="task-1",
                    )
    print("  ✅ approve → schedule_incremental_update")


def test_approve_skips_hook_without_diff():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {"id": "t1", "project_id": "p1", "merged_diff": "", "status": "DELIVERING"}
    project = {"id": "p1", "path": "/tmp/p"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        mock_store.get_project.return_value = project
        mock_store.update_task.return_value = task
        with patch("swarm.brain.runner.resume_task_background"):
            with patch("swarm.brain.runner.register_task_queue"):
                with patch("swarm.knowledge.hooks.schedule_incremental_update") as mock_hook:
                    client = TestClient(app)
                    resp = client.post("/api/tasks/t1/approve")
                    assert resp.status_code == 200
                    mock_hook.assert_not_called()
    print("  ✅ approve skips hook when no diff")


def test_incremental_update_from_task_calls_updater():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from swarm.knowledge.hooks import incremental_update_from_task

    with patch("swarm.knowledge.hooks.enqueue_kb_update", new_callable=AsyncMock) as mock_eq:
        mock_eq.return_value = 99
        with patch("swarm.knowledge.hooks._build_changes", return_value=[object()]):
            result = asyncio.run(
                incremental_update_from_task("p1", "/tmp", "--- a/x\n+++ b/x\n", task_id="t1")
            )
    assert result["status"] == "queued"
    assert result["event_id"] == 99
    mock_eq.assert_awaited_once()
    print("  ✅ incremental_update_from_task → enqueue")


def main() -> int:
    tests = [
        test_approve_schedules_incremental_update,
        test_approve_skips_hook_without_diff,
        test_incremental_update_from_task_calls_updater,
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
    print(f"\n✅ 全部 {len(tests)} 项 knowledge hooks 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
