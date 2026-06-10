#!/usr/bin/env python3
"""ConsistencyChecker + milestone + merge apply block tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_consistency_detects_stale():
    from swarm.knowledge import consistency

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        f = root / "a.py"
        f.write_text("print(1)\n", encoding="utf-8")
        import datetime as dt
        from datetime import timezone

        old = dt.datetime(2020, 1, 1, tzinfo=timezone.utc)

        class FakeCur:
            def execute(self, *a, **k):
                return None

            def fetchall(self):
                return [("a.py", old)]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(consistency, "psycopg") as mock_pg:
            mock_pg.connect.return_value = FakeConn()
            report = consistency.check_project_consistency("p1", str(root))
        assert report["ok"] is True
        assert report["stale_count"] >= 1
    print("  ✅ consistency detects stale file")


def test_save_milestone_report_mock():
    from swarm.project import store

    with patch("swarm.project.store.psycopg.connect") as mock_conn:
        cur = MagicMock()
        cur.fetchone.return_value = (1, "p1", "0", 0.8, 0.6, True, {}, None)
        mock_conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value = cur
        row = store.save_milestone_report(
            project_id="p1",
            phase="0",
            accept_rate=0.8,
            threshold=0.6,
            passed=True,
            report={"total": 5},
        )
        assert row["accept_rate"] == 0.8
    print("  ✅ save_milestone_report")


def test_apply_diff_blocked_on_merge_conflicts():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    client = TestClient(app)
    task = {
        "id": "t-conf",
        "project_id": "p1",
        "description": "x",
        "status": "DELIVERING",
        "merged_diff": "--- a/f\n+++ b/f\n+line\n",
        "merge_conflicts": [{"file_path": "f.py", "message": "overlap"}],
    }
    project = {"id": "p1", "path": "/tmp", "name": "demo"}

    with patch("swarm.api.app.store.get_task", return_value=task), patch(
        "swarm.api.app.store.get_project", return_value=project
    ):
        resp = client.post("/api/tasks/t-conf/apply-diff", json={"check_only": False})
        assert resp.status_code == 409
    print("  ✅ apply-diff 409 on merge conflicts")


def test_token_limit_helper():
    from swarm.project import store

    cfg = MagicMock()
    cfg.max_task_tokens = 100
    with patch.object(store, "estimate_token_usage", return_value={"total": 9999999}), patch.object(
        store, "update_task", return_value={}
    ) as mock_upd, patch("swarm.config.settings.get_config", return_value=cfg):
        ok, usage = store.check_task_token_limit("t1", description="big")
        assert ok is False
        mock_upd.assert_called_once()
    print("  ✅ check_task_token_limit")


def main():
    print("\n🐝 P0-P2 gap tests\n")
    test_consistency_detects_stale()
    test_save_milestone_report_mock()
    test_apply_diff_blocked_on_merge_conflicts()
    test_token_limit_helper()
    print("\n✅ 全部 P0-P2 gap 测试通过\n")


if __name__ == "__main__":
    main()
