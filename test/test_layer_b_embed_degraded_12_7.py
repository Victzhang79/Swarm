"""12.7 修复回归测试：embedding 不可用时 Layer B 优雅降级为 BM25 关键词检索。

历史缺口：_retrieve_layer_b 拿到零向量(embed 服务挂掉的回退)后仍继续向量检索，
零向量召回 = 噪声，污染注入给 Brain 的上下文，且用户无明确信号。

修复后：检测到零向量/None → 改走 semantic.bm25_only_search（scroll + BM25），
保住关键词检索能力（优雅降级，而非跳过或返回噪声）。

本测试用 mock 隔离 SemanticIndexer，不连真 Qdrant/embed 服务，纯逻辑验证。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from swarm.knowledge.retriever import SwarmRetriever, _is_zero_vec
from swarm.knowledge.semantic_index import BGE_M3_DIMENSION


# ── _is_zero_vec 工具 ────────────────────────────────────

def test_is_zero_vec_detects_zero():
    assert _is_zero_vec([0.0] * BGE_M3_DIMENSION) is True


def test_is_zero_vec_detects_none():
    assert _is_zero_vec(None) is True
    assert _is_zero_vec([]) is True


def test_is_zero_vec_rejects_nonzero():
    v = [0.0] * BGE_M3_DIMENSION
    v[0] = 0.7
    assert _is_zero_vec(v) is False


# ── Layer B 降级路径 ─────────────────────────────────────

def _make_retriever_with_mock_semantic(embed_returns):
    """构造一个挂了 mock SemanticIndexer 的 retriever。embed_returns 为 _embed_fn 返回值。"""
    r = SwarmRetriever()
    sem = MagicMock()
    sem._embed_fn = AsyncMock(return_value=embed_returns)
    sem.search = AsyncMock(return_value=[{"id": "VEC", "content": "向量召回结果", "score": 0.9}])
    sem.search_with_rerank = AsyncMock(
        return_value=[{"id": "VEC", "content": "向量召回结果", "score": 0.9}]
    )
    sem.bm25_only_search = AsyncMock(
        return_value=[{"id": "BM25", "content": "关键词降级结果", "score": 0.5, "bm25_score": 0.5}]
    )
    r._semantic = sem
    return r, sem


def test_layer_b_degrades_to_bm25_on_zero_vector():
    """核心修复点：embed 返回零向量 → Layer B 走 bm25_only_search，不走向量检索。"""
    zero = [[0.0] * BGE_M3_DIMENSION]
    r, sem = _make_retriever_with_mock_semantic(zero)

    results = asyncio.run(
        r._retrieve_layer_b("_test_proj", "给用户列表加排序功能", keywords=["用户", "排序"])
    )

    # 走了 BM25 降级
    sem.bm25_only_search.assert_awaited_once()
    # 没有走向量检索路径
    sem.search.assert_not_awaited()
    sem.search_with_rerank.assert_not_awaited()
    # 返回的是降级结果，不是零向量噪声
    assert results and results[0]["id"] == "BM25"


def test_layer_b_uses_vector_when_embed_healthy():
    """反例：embed 正常(非零向量) → 走正常向量检索，不降级。"""
    healthy = [[0.0] * BGE_M3_DIMENSION]
    healthy[0][0] = 0.8  # 非零
    r, sem = _make_retriever_with_mock_semantic(healthy)

    asyncio.run(
        r._retrieve_layer_b("_test_proj", "给用户列表加排序功能", keywords=["用户", "排序"])
    )

    # 走了向量检索（search_with_rerank 全局补充必被调用）
    sem.search_with_rerank.assert_awaited()
    # 没有降级
    sem.bm25_only_search.assert_not_awaited()


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n=== 12.7 Layer B graceful degradation: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
