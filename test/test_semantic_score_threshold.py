"""P1-10 回归：semantic_score_threshold 接线到检索——低于阈值的语义结果被丢弃。

纯单元：stub SemanticIndexer 的 embed/search_with_rerank，不依赖真 Qdrant。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from swarm.config.settings import KnowledgeConfig
from swarm.knowledge.retriever import SwarmRetriever


def _retriever(threshold: float) -> SwarmRetriever:
    kb = KnowledgeConfig()
    kb.semantic_score_threshold = threshold
    r = SwarmRetriever(kb_config=kb)
    sem = AsyncMock()
    sem._embed_fn = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])  # 非零 → 走向量路径
    sem.search_with_rerank = AsyncMock(return_value=[
        {"id": "low", "score": 0.4, "file_path": "a.py"},
        {"id": "high", "score": 0.6, "file_path": "b.py"},
    ])
    r._semantic = sem
    return r


@pytest.mark.asyncio
async def test_threshold_drops_low_score():
    r = _retriever(threshold=0.5)
    out = await r._retrieve_layer_b("_test_p1_10", "query")
    ids = {x["id"] for x in out}
    assert ids == {"high"}, f"低于阈值的应被丢弃，得 {ids}"


@pytest.mark.asyncio
async def test_threshold_zero_keeps_all():
    r = _retriever(threshold=0.0)
    out = await r._retrieve_layer_b("_test_p1_10", "query")
    ids = {x["id"] for x in out}
    assert ids == {"low", "high"}, f"阈值 0 应不过滤，得 {ids}"


@pytest.mark.asyncio
async def test_threshold_exempts_kw_union_candidates():
    """R65B-T3 猎手 F1（CONFIRMED HIGH）：BM25 关键词臂并集候选（kw_union，score
    占位 0.0）无稠密相似度——向量阈值对它是范畴错误，阈值一开整臂静默灭失，
    恰好杀掉并集要救的关键词精确命中。必须豁免。"""
    r = _retriever(threshold=0.5)
    r._semantic.search_with_rerank = AsyncMock(return_value=[
        {"id": "low", "score": 0.4, "file_path": "a.py"},
        {"id": "high", "score": 0.6, "file_path": "b.py"},
        {"id": "kw", "score": 0.0, "kw_union": True, "bm25_score": 3.2,
         "file_path": "c.py"},
    ])
    out = await r._retrieve_layer_b("_test_p1_10", "query")
    ids = {x["id"] for x in out}
    assert ids == {"high", "kw"}, \
        f"kw_union 候选必须豁免向量阈值（低稠密分仍照常过滤），得 {ids}"
