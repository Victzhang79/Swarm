"""12.4 修复回归测试：Qdrant payload 加 index_version + index_source 溯源标记。

问题：预处理全量(CodeGraph 符号嵌入)与增量(SemanticIndexer 语义分块)两条路径写入
Qdrant 的 payload 形态不同，首次增量更新后向量库内容形态变化难排查（无版本/来源标记）。

修复（轻量版，纯标记不动检索）：两条路径 payload 各加 index_version + index_source。

本测试用 mock 隔离 Qdrant client，捕获 upsert 的 payload 验证溯源字段，不连真服务。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from swarm.knowledge.semantic_index import (
    INDEX_SOURCE_CODEGRAPH,
    INDEX_SOURCE_SEMANTIC,
    INDEX_VERSION,
    Chunk,
    SemanticIndexer,
)


def test_index_source_constants_distinct():
    """两条路径来源常量必须可区分。"""
    assert INDEX_SOURCE_SEMANTIC != INDEX_SOURCE_CODEGRAPH
    assert INDEX_VERSION  # 非空


def test_semantic_index_payload_has_provenance():
    """SemanticIndexer.index_chunks 写入的 payload 含 index_version + index_source=semantic。"""
    idx = SemanticIndexer()
    # mock Qdrant client + embed
    fake_client = MagicMock()
    captured = {}

    async def _capture_upsert(collection_name, points):
        captured["points"] = points

    fake_client.upsert = AsyncMock(side_effect=_capture_upsert)
    idx._client = fake_client
    idx._embed_fn = AsyncMock(return_value=[[0.1] * 1024])

    chunk = Chunk(
        content="def foo(): pass",
        chunk_type="method",
        file_path="src/a.py",
        module_name="a",
    )
    asyncio.run(idx.index_chunks("_test_proj_12_4", [chunk]))

    pts = captured.get("points")
    assert pts and len(pts) == 1, "应写入 1 个 point"
    payload = pts[0].payload
    assert payload["index_version"] == INDEX_VERSION
    assert payload["index_source"] == INDEX_SOURCE_SEMANTIC
    # 原有字段不丢
    assert payload["chunk_type"] == "method"
    assert payload["file_path"] == "src/a.py"


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
    print(f"\n=== 12.4 index provenance: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
