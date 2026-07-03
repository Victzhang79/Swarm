"""P1-13 回归：get_all_mistakes/get_all_successes 须过滤 merged/archived/dismissed。

后果不止"检索含旧碎片"——get_memory_health 把 active=len(get_all) 且 stored=active+merged，
若 get_all 含 merged 则 merged 被【双计】、dedup_rate 失真。此测试锁定过滤 + 计数正确。

触真实 PG，_test_ 前缀隔离 + try/finally 清理。需本地 PG。
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig
from swarm.memory.decay import MemoryDecay
from swarm.memory.store import MemoryStore


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="PG 不可达")

_PID = f"_test_p1_13_{uuid.uuid4().hex[:8]}"


def _conn():
    return psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True)


def _seed():
    with _conn() as conn, conn.cursor() as cur:
        # 2 条 active + 1 条 merged
        cur.execute(
            "INSERT INTO mem_mistakes (project_id, error_type, description, decay_weight, metadata_json) "
            "VALUES (%s,'compile_error','a',1.0,'{}'::jsonb),"
            "       (%s,'test_failure','b',1.0,'{}'::jsonb),"
            "       (%s,'logic_error','c',1.0,'{\"status\":\"merged\"}'::jsonb)",
            (_PID, _PID, _PID),
        )
        cur.execute(
            "INSERT INTO mem_successes (project_id, pattern_name, description, decay_weight, metadata_json) "
            "VALUES (%s,'p1','d',1.0,'{}'::jsonb),"
            "       (%s,'p2','e',1.0,'{\"status\":\"merged\"}'::jsonb)",
            (_PID, _PID),
        )


def _cleanup():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM mem_mistakes WHERE project_id = %s", (_PID,))
        cur.execute("DELETE FROM mem_successes WHERE project_id = %s", (_PID,))


def test_get_all_filters_merged_and_health_counts_correct():
    _seed()

    async def _run():
        store = MemoryStore()
        await store.connect()
        try:
            mistakes = await store.get_all_mistakes(_PID, min_weight=0.0)
            successes = await store.get_all_successes(_PID, min_weight=0.0)
            health = await MemoryDecay(store).get_memory_health(_PID)
            return mistakes, successes, health
        finally:
            await store.close()

    try:
        mistakes, successes, health = asyncio.run(_run())

        # get_all_* 排除 merged
        assert len(mistakes) == 2, f"get_all_mistakes 应排除 merged，得 {len(mistakes)}"
        assert len(successes) == 1, f"get_all_successes 应排除 merged，得 {len(successes)}"

        # get_memory_health 计数正确（active=非merged，stored=active+merged，dedup=merged/stored）
        m = health["mistakes"]
        assert m["active"] == 2 and m["merged"] == 1 and m["stored"] == 3, m
        assert abs(m["dedup_rate"] - 1 / 3) < 1e-3, m  # dedup_rate round(,4)
        s = health["successes"]
        assert s["active"] == 1 and s["merged"] == 1 and s["stored"] == 2, s
    finally:
        _cleanup()
