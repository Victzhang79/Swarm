#!/usr/bin/env python3
"""残留审查回归：
- reindex_file_atomic 在 embedding 不可用(零向量)时必须抛 EmbeddingUnavailableError 且【不 prune】
  旧 chunk（守住 write-then-prune 不误删有效数据的治本）。
- _read_text_any / search_in_files 的 workspace 边界复校（symlink 不能读出界）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest


# ── reindex_file_atomic: 零向量不写入、不 prune ──────────────────────────
def test_reindex_file_atomic_raises_and_skips_prune_on_zero_vector():
    from swarm.knowledge.semantic_index import (
        BGE_M3_DIMENSION,
        EmbeddingUnavailableError,
        SemanticIndexer,
    )

    idx = SemanticIndexer.__new__(SemanticIndexer)  # 不走 connect，注入桩
    pruned = {"called": False}
    upserted = {"called": False}

    class _FakeClient:
        async def upsert(self, **kw):
            upserted["called"] = True

        async def delete(self, **kw):
            pruned["called"] = True

    idx._client = _FakeClient()
    idx._collection_name = "swarm_kb"
    # embedding 服务不可用 → 返回零向量占位
    async def _zero_embed(texts):
        return [[0.0] * BGE_M3_DIMENSION for _ in texts]
    idx._embed_fn = _zero_embed

    # 用真实切分产出至少一个 chunk
    from types import SimpleNamespace
    idx._kb_config = SimpleNamespace(chunk_size=100, chunk_overlap=10)

    async def _run():
        with pytest.raises(EmbeddingUnavailableError):
            await idx.reindex_file_atomic(
                "proj", "def foo():\n    return 1\n", "a.py", module_name="m"
            )

    asyncio.run(_run())
    assert upserted["called"] is False, "零向量绝不能 upsert 进 Qdrant"
    assert pruned["called"] is False, "index 失败时绝不能 prune（否则误删旧有效 chunk）"


# ── _read_text_any / search_in_files: workspace 边界 ──────────────────────
def _make_ws_with_escape_symlink():
    ws = tempfile.mkdtemp(prefix="swarm_ws_")
    outside = tempfile.mkdtemp(prefix="swarm_outside_")
    secret = os.path.join(outside, "secret.txt")
    with open(secret, "w") as f:
        f.write("TOP SECRET OUTSIDE WORKSPACE\n")
    link = os.path.join(ws, "escape.txt")
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        return None, None, None
    return ws, link, secret


def test_read_text_any_blocks_symlink_escape(monkeypatch):
    import swarm.tools.file_tools as ft
    from swarm.tools.file_tools import WorkspaceEscapeError

    ws, link, _secret = _make_ws_with_escape_symlink()
    if ws is None:
        pytest.skip("symlink not supported")
    # _resolve_read/_resolve 在函数内 `from swarm.tools.paths import workspace_root`，
    # 故须 patch 源模块而非 file_tools 的引用。
    import swarm.tools.paths as paths
    monkeypatch.setattr(paths, "workspace_root", lambda: Path(ws).resolve())
    monkeypatch.setattr(ft, "_resolve_sandbox", lambda p: None)  # 强制本地分支

    with pytest.raises(WorkspaceEscapeError):
        ft._read_text_any(link)


if __name__ == "__main__":
    test_reindex_file_atomic_raises_and_skips_prune_on_zero_vector()
    print("ok")
