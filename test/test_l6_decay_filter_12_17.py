"""12.17 修复回归测试：L6 成功模式检索按 decay_weight 过滤（对齐 L5）。

历史 bug：search_successes 仅过滤 archived/dismissed，不过滤 decay_weight，
导致已衰减到接近 0 的陈旧成功模式仍被向量召回注入 prompt。修复后加
`AND decay_weight > 0.05`（与 L5 search 一致）。

本测试触真实 PG（L6 表带 pgvector），严格遵守测试铁律：
- 仅使用 _test_ 前缀隔离的 project_id；
- try/finally 中按 project_id 清理本测试写入的行；
- 绝不 set/delete 任何生产标识符。
需要本地 PG（postgresql://localhost:5432/swarm，pgvector 已装）。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from swarm.memory.store import BGE_M3_DIMENSION, MemoryStore, SuccessEntry


# 该测试专用、唯一、绝不与生产撞车的隔离 project_id
_TEST_PROJECT_ID = f"_test_12_17_decay_{uuid.uuid4().hex[:8]}"


def _unit_vector() -> list[float]:
    """固定非零向量：第 0 维为 1，其余 0。避免依赖真实 embed 服务，
    同时保证不是零向量（不会被 _is_zero_vector 标记为 placeholder）。"""
    v = [0.0] * BGE_M3_DIMENSION
    v[0] = 1.0
    return v


async def _run() -> None:
    store = MemoryStore()
    await store.connect()
    written_ids: list[int] = []
    try:
        # 写两条 L6，显式给非零 embedding（绕过 embed 服务）
        fresh_id = await store.write_success(
            _TEST_PROJECT_ID,
            SuccessEntry(
                pattern_name="fresh-pattern",
                description="活跃的成功模式，权重应保持",
                approach="approach-fresh",
                task_id=f"{_TEST_PROJECT_ID}_task_fresh",
                embedding=_unit_vector(),
            ),
        )
        stale_id = await store.write_success(
            _TEST_PROJECT_ID,
            SuccessEntry(
                pattern_name="stale-pattern",
                description="陈旧的成功模式，已衰减应被过滤",
                approach="approach-stale",
                task_id=f"{_TEST_PROJECT_ID}_task_stale",
                embedding=_unit_vector(),
            ),
        )
        written_ids += [fresh_id, stale_id]

        # 把 stale 衰减到阈值以下（0.01 < 0.05）
        await store.update_success_decay_weight(stale_id, 0.01)

        # 检索（query 任意，显式传非零向量绕过 embed 服务——CI 无 bge-m3 服务时
        # 文字 query 会被 embed 成零向量并短路返空，导致 CI-only 失败；本测试只验
        # decay 过滤逻辑，不该依赖外部 embed 服务）
        results = await store.query_successes(
            _TEST_PROJECT_ID, query="pattern", top_k=10, query_vector=_unit_vector()
        )
        ids = {r["id"] for r in results}

        assert fresh_id in ids, "活跃成功模式应被检索到"
        assert stale_id not in ids, "已衰减(decay_weight<0.05)的成功模式必须被过滤 (12.17)"
    finally:
        # 清理：仅删本测试隔离 project 的数据
        conn = store._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM mem_successes WHERE project_id = %s", (_TEST_PROJECT_ID,)
            )
        await store.close()


def test_l6_search_filters_decayed():
    asyncio.run(_run())


if __name__ == "__main__":
    try:
        asyncio.run(_run())
        print("  ✅ test_l6_search_filters_decayed")
        print("\n=== 12.17 L6 decay filter: 1/1 passed ===")
    except AssertionError as e:
        print(f"  ❌ {e}")
        raise
