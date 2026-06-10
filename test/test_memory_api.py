#!/usr/bin/env python3
"""L1 用户画像 API 冒烟测试（mock PG / validate）"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _mock_pg_conn(fetchone_results: list, rowcount: int = 1):
    """构造 _get_pg_conn 的 context manager mock"""
    cursor = MagicMock()
    cursor.fetchone.side_effect = fetchone_results
    cursor.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _cm():
        yield conn

    return _cm, cursor


def _disable_rbac():
    cfg = MagicMock()
    cfg.rbac_enabled = False
    cfg.api_key = ""
    return patch("swarm.api.auth.get_config", return_value=cfg)


def test_get_profile_empty():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    cm, _ = _mock_pg_conn([None, None, None])

    with _disable_rbac():
        with patch("swarm.api.app._validate_project"):
            with patch("swarm.api.app._get_pg_conn", cm):
                client = TestClient(app)
                resp = client.get("/api/projects/p1/memories/profile")
                assert resp.status_code == 200, resp.text
                data = resp.json()
                assert data["user_id"] == "anonymous"
                assert data["project_id"] == "p1"
                assert data["profile_json"] == {}
    print("  ✅ GET /memories/profile (empty)")


def test_get_profile_existing():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    profile = {"preferences": {"style": "concise"}, "notes": "test"}
    cm, _ = _mock_pg_conn([(profile,)])

    with _disable_rbac():
        with patch("swarm.api.app._validate_project"):
            with patch("swarm.api.app._get_pg_conn", cm):
                client = TestClient(app)
                resp = client.get("/api/projects/p2/memories/profile")
                assert resp.status_code == 200, resp.text
                assert resp.json()["profile_json"] == profile
    print("  ✅ GET /memories/profile (existing)")


def test_put_profile_upsert():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    new_profile = {"language": "python", "framework": "fastapi"}
    cm, cursor = _mock_pg_conn([(new_profile,)])

    with _disable_rbac():
        with patch("swarm.api.app._validate_project"):
            with patch("swarm.api.app._get_pg_conn", cm):
                client = TestClient(app)
                resp = client.put(
                    "/api/projects/p3/memories/profile",
                    json={"profile_json": new_profile},
                )
                assert resp.status_code == 200, resp.text
                data = resp.json()
                assert data["updated"] is True
                assert data["profile_json"] == new_profile
                assert data["user_id"] == "anonymous"
                cursor.execute.assert_called_once()
    print("  ✅ PUT /memories/profile")


def test_behavior_hotspots_endpoint():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    rows = [
        ("src/main.py", 5, None),
        ("lib/util.py", 3, None),
    ]
    cursor = MagicMock()
    cursor.fetchall.return_value = rows

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def _cm():
        yield conn

    with _disable_rbac():
        with patch("swarm.api.app._validate_project"):
            with patch("swarm.api.app._get_pg_conn", _cm):
                client = TestClient(app)
                resp = client.get("/api/projects/p1/knowledge/behavior-hotspots?top_k=10")
            assert resp.status_code == 200, resp.text
            hotspots = resp.json().get("hotspots", [])
            assert len(hotspots) == 2
            assert hotspots[0]["file_path"] == "src/main.py"
            assert hotspots[0]["mod_count"] == 5
            assert hotspots[0]["type"] == "hotspot"
    print("  ✅ GET /knowledge/behavior-hotspots")


def main() -> int:
    tests = [
        test_get_profile_empty,
        test_get_profile_existing,
        test_put_profile_upsert,
        test_behavior_hotspots_endpoint,
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
        print(f"\n{failed} test(s) failed")
        return 1
    print(f"\nAll {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
