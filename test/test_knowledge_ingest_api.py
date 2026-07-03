#!/usr/bin/env python3
"""KB 采集端点 POST /api/projects/{pid}/knowledge/ingest 测试。

绝不真落 Qdrant swarm_kb：
  - dry_run=True 走纯预览（pipeline 内部恒不调 index_chunks）。
  - 非 dry_run 路径用 mock 的 indexer + mock 的 _run_on_kb_loop，验证编排正确，
    依然不触达真实库。
  - 远端源（无 token）验证返回 400 + 接入提示。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _write_sample(tmp_path: Path) -> str:
    # P1-17：KB ingest local 现要求 file_paths 落在 uploads 内（同 #5(b) LFI 防护，反映真实
    # /api/uploads 契约）。样例写进 uploads 目录而非裸 tmp。
    from swarm.api.routers.upload import _uploads_root
    d = _uploads_root() / "test_ingest_round22"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "sample.md"
    p.write_text("# Title\n\nHello world. " * 50, encoding="utf-8")
    return str(p.resolve())


def test_ingest_local_dry_run(tmp_path):
    """dry_run=True：解析+切分预览，indexed_chunks 恒为 0，绝不落库。"""
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    fp = _write_sample(tmp_path)
    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = {"id": "p1", "path": str(tmp_path)}
        with patch("swarm.api.app._validate_project"):
            client = TestClient(app)
            resp = client.post(
                "/api/projects/p1/knowledge/ingest",
                json={"file_paths": [fp], "source_type": "local", "dry_run": True},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["dry_run"] is True
            assert data["total_docs"] == 1
            assert data["parsed_docs"] == 1
            assert data["indexed_chunks"] == 0  # dry_run 绝不落库
            assert data["total_chunks"] > 0
            assert data["docs"][0]["status"] == "parsed"
    print("  ✅ POST /knowledge/ingest dry_run=True 预览不落库")


def test_ingest_local_real_mocked_indexer(tmp_path):
    """非 dry_run：用 mock indexer + mock _run_on_kb_loop，验证落库编排，不触达真实 Qdrant。"""
    import asyncio

    from fastapi.testclient import TestClient
    from swarm.api.app import app

    fp = _write_sample(tmp_path)

    class FakeSemantic:
        async def index_chunks(self, project_id, chunks, batch_size=64):
            return len(chunks)  # 假装落库成功，不碰真实库

    class FakeRetriever:
        _semantic = FakeSemantic()

    async def fake_get_retriever():
        return FakeRetriever()

    def fake_run_on_kb_loop(coro):
        # 直接在临时 loop 跑协程（mock 的 indexer 不碰真实连接，安全）
        return asyncio.run(coro)

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = {"id": "p1", "path": str(tmp_path)}
        with patch("swarm.api.app._validate_project"), \
                patch("swarm.knowledge.service.get_retriever", fake_get_retriever), \
                patch("swarm.knowledge.service._run_on_kb_loop", fake_run_on_kb_loop):
            client = TestClient(app)
            resp = client.post(
                "/api/projects/p1/knowledge/ingest",
                json={"file_paths": [fp], "source_type": "local", "dry_run": False},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["dry_run"] is False
            assert data["parsed_docs"] == 1
            assert data["indexed_chunks"] > 0  # mock indexer 报告落库数
    print("  ✅ POST /knowledge/ingest dry_run=False（mock indexer）落库编排")


def test_ingest_local_missing_paths(tmp_path):
    """source_type=local 但无 file_paths → 400。"""
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = {"id": "p1", "path": str(tmp_path)}
        with patch("swarm.api.app._validate_project"):
            client = TestClient(app)
            resp = client.post(
                "/api/projects/p1/knowledge/ingest",
                json={"file_paths": [], "source_type": "local", "dry_run": True},
            )
            assert resp.status_code == 400, resp.text
            assert "file_paths" in resp.json()["detail"]
    print("  ✅ POST /knowledge/ingest local 缺 file_paths → 400")


def test_ingest_remote_no_token(tmp_path):
    """远端源无 token → 400 + 接入提示（NotImplementedError 被 catch）。"""
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = {"id": "p1", "path": str(tmp_path)}
        with patch("swarm.api.app._validate_project"):
            # 确保飞书 env 不存在 → adapter 抛 NotImplementedError
            with patch.dict("os.environ", {}, clear=False) as _env:
                import os
                for k in ("SWARM_INGEST_FEISHU_APP_ID", "SWARM_INGEST_FEISHU_APP_SECRET"):
                    os.environ.pop(k, None)
                client = TestClient(app)
                resp = client.post(
                    "/api/projects/p1/knowledge/ingest",
                    json={"source_type": "feishu", "dry_run": True},
                )
                assert resp.status_code == 400, resp.text
                detail = resp.json()["detail"]
                assert "feishu" in detail.lower() or "FEISHU" in detail
    print("  ✅ POST /knowledge/ingest feishu 无 token → 400 + 接入提示")


def test_ingest_bad_source_type(tmp_path):
    """未知 source_type → 400。"""
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = {"id": "p1", "path": str(tmp_path)}
        with patch("swarm.api.app._validate_project"):
            client = TestClient(app)
            resp = client.post(
                "/api/projects/p1/knowledge/ingest",
                json={"source_type": "dropbox", "dry_run": True},
            )
            assert resp.status_code == 400, resp.text
    print("  ✅ POST /knowledge/ingest 未知 source_type → 400")


def main() -> int:
    import tempfile

    tests = [
        test_ingest_local_dry_run,
        test_ingest_local_real_mocked_indexer,
        test_ingest_local_missing_paths,
        test_ingest_remote_no_token,
        test_ingest_bad_source_type,
    ]
    failed = 0
    for fn in tests:
        try:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\n✅ 全部 {len(tests)} 项 ingest API 测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
