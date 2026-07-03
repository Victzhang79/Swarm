"""P1-14 回归：手工建错题/成功模式须 embed 真向量，不写零向量占位。

- embed 可用 → 200 且行 embedding 非零（可被语义召回）。
- embed 不可用(零向量) → 503 且不写行（不落不可召回的垃圾）。

触真实 PG（端点内部 MemoryStore.connect），连不上 skip。RBAC 由 conftest 关。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="PG 不可达")

_PID = f"_test_p1_14_{uuid.uuid4().hex[:8]}"
_NONZERO = [0.1] * 1024
_ZERO = [0.0] * 1024


def _conn():
    return psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True)


def _count(table: str) -> int:
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = %s", (_PID,))
        return cur.fetchone()[0]


def _cleanup():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM mem_mistakes WHERE project_id = %s", (_PID,))
        cur.execute("DELETE FROM mem_successes WHERE project_id = %s", (_PID,))


def _client():
    from fastapi.testclient import TestClient

    from swarm.api.app import app
    return TestClient(app)


def _ensure_tables():
    import asyncio

    from swarm.memory.store import MemoryStore

    async def _mk():
        store = MemoryStore()
        await store.connect()
        try:
            await store.ensure_tables()
        finally:
            await store.close()

    asyncio.run(_mk())


def test_create_mistake_embeds_real_vector():
    _ensure_tables()
    try:
        with patch("swarm.api.app._validate_project"), \
             patch("swarm.memory.store.MemoryStore._default_embed",
                   new=AsyncMock(return_value=[_NONZERO])):
            resp = _client().post(
                f"/api/projects/{_PID}/memories/mistakes",
                json={"error_type": "compile_error", "description": "x"},
            )
        assert resp.status_code == 200, resp.text
        assert _count("mem_mistakes") == 1
        # 行 embedding 非零（能被语义召回）
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT embedding IS NOT NULL FROM mem_mistakes WHERE project_id = %s", (_PID,)
            )
            assert cur.fetchone()[0] is True
    finally:
        _cleanup()


def test_create_mistake_embed_unavailable_returns_503_no_row():
    _ensure_tables()
    try:
        with patch("swarm.api.app._validate_project"), \
             patch("swarm.memory.store.MemoryStore._default_embed",
                   new=AsyncMock(return_value=[_ZERO])):
            resp = _client().post(
                f"/api/projects/{_PID}/memories/mistakes",
                json={"error_type": "compile_error", "description": "x"},
            )
        assert resp.status_code == 503, resp.text
        assert _count("mem_mistakes") == 0, "embed 不可用时不得写行"
    finally:
        _cleanup()


def test_create_success_embed_unavailable_returns_503_no_row():
    _ensure_tables()
    try:
        with patch("swarm.api.app._validate_project"), \
             patch("swarm.memory.store.MemoryStore._default_embed",
                   new=AsyncMock(return_value=[_ZERO])):
            resp = _client().post(
                f"/api/projects/{_PID}/memories/successes",
                json={"pattern_name": "p", "description": "d"},
            )
        assert resp.status_code == 503, resp.text
        assert _count("mem_successes") == 0
    finally:
        _cleanup()
