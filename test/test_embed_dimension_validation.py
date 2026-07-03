"""P1-9 回归：向量维度从配置读取 + 写入前校验，不符 fail-closed。

纯单元：mock Qdrant client + embed_fn，不依赖真 Qdrant/embed 服务。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.config.settings import KnowledgeConfig
from swarm.knowledge.semantic_index import (
    BGE_M3_DIMENSION,
    Chunk,
    EmbeddingDimensionMismatchError,
    SemanticIndexer,
)


def test_default_embed_dimension_is_1024():
    assert KnowledgeConfig().embed_dimension == BGE_M3_DIMENSION == 1024


def _indexer(dim: int) -> SemanticIndexer:
    kb = KnowledgeConfig()
    kb.embed_dimension = dim
    idx = SemanticIndexer(kb_config=kb)
    assert idx._dim == dim  # 从配置读取（单一来源）
    client = MagicMock()
    client.upsert = AsyncMock()
    idx._client = client
    return idx


def _chunk() -> Chunk:
    return Chunk(content="hello world", chunk_type="free_text", file_path="a.py", start_line=1)


@pytest.mark.asyncio
async def test_dimension_mismatch_fails_closed_no_upsert():
    idx = _indexer(dim=8)
    idx._embed_fn = AsyncMock(return_value=[[0.1] * 4])  # 实际 4 != 配置 8
    with pytest.raises(EmbeddingDimensionMismatchError):
        await idx.index_chunks("_test_p1_9", [_chunk()])
    idx._client.upsert.assert_not_called()  # 错维向量绝不落库


@pytest.mark.asyncio
async def test_matching_dimension_upserts():
    idx = _indexer(dim=8)
    idx._embed_fn = AsyncMock(return_value=[[0.1] * 8])  # 匹配
    n = await idx.index_chunks("_test_p1_9", [_chunk()])
    assert n == 1
    idx._client.upsert.assert_awaited()
