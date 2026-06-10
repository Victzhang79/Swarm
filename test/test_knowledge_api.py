#!/usr/bin/env python3
"""知识库搜索 / 预处理 API 冒烟测试（mock store）"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_symbols_search_endpoint():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    mock_rows = [
        {"name": "load_dotenv", "kind": "function", "file_path": "src/main.py", "line": 10},
    ]

    class FakeIndexer:
        async def connect(self):
            return None

        async def close(self):
            return None

        async def query_symbols_by_name(self, project_id: str, q: str):
            return mock_rows

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = {"id": "p1", "path": "/tmp"}
        with patch("swarm.api.app._validate_project"):
            with patch(
                "swarm.knowledge.structure_index.StructureIndexer",
                return_value=FakeIndexer(),
            ):
                client = TestClient(app)
                resp = client.get("/api/projects/p1/knowledge/symbols?q=load")
                assert resp.status_code == 200, resp.text
                assert resp.json().get("symbols") == mock_rows
    print("  ✅ GET /knowledge/symbols")


def test_preprocess_status_endpoint():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = {
            "id": "p1",
            "status": "READY",
            "graph_status": "INDEXED",
        }
        mock_store.get_progress.return_value = {
            "phase": "complete",
            "phase_progress": 100.0,
            "message": "done",
        }
        with patch("swarm.api.app._validate_project"):
            client = TestClient(app)
            resp = client.get("/api/projects/p1/preprocess/status")
            assert resp.status_code == 200, resp.text
            assert resp.json()["project_status"] == "READY"
    print("  ✅ GET /preprocess/status")


def main() -> int:
    tests = [test_symbols_search_endpoint, test_preprocess_status_endpoint]
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
    print(f"\n✅ 全部 {len(tests)} 项 knowledge API 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
