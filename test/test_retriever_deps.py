#!/usr/bin/env python3
"""P0 — A→依赖图扩展检索测试"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge.retriever import SwarmRetriever


async def test_expand_dependency_files():
    retriever = SwarmRetriever()
    mock_struct = MagicMock()
    mock_struct.query_transitive_deps = AsyncMock(
        side_effect=lambda pid, fp, max_depth=2: {
            "a.py": ["b.py", "c.py"],
            "x.py": [],
        }.get(fp, [])
    )
    retriever.set_structure_indexer(mock_struct)

    expanded = await retriever._expand_dependency_files("proj-1", ["a.py", "x.py"])
    assert expanded == ["b.py", "c.py"]
    assert mock_struct.query_transitive_deps.await_count == 2


async def test_retrieve_includes_deps_in_context():
    retriever = SwarmRetriever()
    mock_struct = MagicMock()
    mock_struct.query_symbols_by_name = AsyncMock(return_value=[
        {"file_path": "main.py", "symbol_name": "Main"},
    ])
    mock_struct.query_symbols_by_class = AsyncMock(return_value=[])
    mock_struct.query_symbols_by_file_keyword = AsyncMock(return_value=[])
    mock_struct.query_transitive_deps = AsyncMock(return_value=["util.py"])
    retriever.set_structure_indexer(mock_struct)

    # 跳过其他层与 meta 加载
    retriever._load_project_meta = AsyncMock(return_value={})
    retriever._semantic = None
    retriever._norms = None
    retriever._behavior = None
    retriever._memory = None

    result = await retriever.retrieve_for_brain("fix Main class", "proj-1")
    ctx = result.context
    assert "util.py" in ctx.get("dependency_files", [])
    assert "util.py" in ctx.get("affected_files", [])
    assert result.stats.get("deps_expanded_count") == 1


def main():
    asyncio.run(test_expand_dependency_files())
    asyncio.run(test_retrieve_includes_deps_in_context())
    print("test_retriever_deps: all passed")


if __name__ == "__main__":
    main()
