#!/usr/bin/env python3
"""Brain 知识检索 & learn 落库集成测试"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_format_layer_items():
    from swarm.knowledge.service import format_layer_items

    items = format_layer_items(
        "struct",
        [{"symbol_name": "foo", "file_path": "a.py", "signature": "def foo(): pass"}],
        5,
    )
    assert items[0]["title"] == "foo (a.py)"
    assert "def foo" in items[0]["content"]
    print("  ✅ format_layer_items")


def test_knowledge_tool_without_project():
    from swarm.knowledge.service import set_worker_context
    from swarm.tools.knowledge_tools import query_knowledge_base

    set_worker_context(None)
    out = query_knowledge_base.invoke({"query": "test", "top_k": 3})
    assert "project_id" in out
    print("  ✅ knowledge_tool — 无 project_id 时拒绝")


def test_knowledge_tool_with_mock_retriever():
    from swarm.knowledge.service import set_worker_context
    from swarm.tools.knowledge_tools import query_knowledge_base

    mock_context = {
        "struct": [{"symbol_name": "parse", "file_path": "parser.py", "signature": "def parse"}],
        "semantic": [],
        "norms": [{"title": "规范", "content": "用 type hints", "priority": 8}],
        "behavior": [],
        "mistakes": [],
        "successes": [],
    }

    with patch(
        "swarm.tools.knowledge_tools.retrieve_knowledge_sync",
        return_value=(mock_context, {"struct_count": 1}),
    ):
        set_worker_context("proj-1")
        out = query_knowledge_base.invoke({"query": "parser", "layers": ["struct", "norms"], "top_k": 3})

    assert "parser.py" in out
    assert "规范" in out
    print("  ✅ knowledge_tool — mock 检索输出正常")


async def _test_analyze_retrieval_async():
    from swarm.brain.nodes import analyze
    from swarm.types import KnowledgeContext

    mock_context: KnowledgeContext = {
        "struct": [{"symbol_name": "main", "file_path": "main.py"}],
        "semantic": [],
        "norms": [],
        "behavior": [],
        "mistakes": [{"description": "past error", "error_type": "logic_error"}],
        "successes": [],
    }

    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        mock_response = MagicMock()
        mock_response.content = '{"complexity": "simple", "reasoning": "test", "key_risks": [], "suggested_subtask_count": 1}'
        mock_llm.return_value.ainvoke = AsyncMock(return_value=mock_response)

        with patch(
            "swarm.knowledge.service.retrieve_knowledge",
            new=AsyncMock(return_value=(mock_context, {"struct_count": 1, "mistakes_count": 1})),
        ):
            result = await analyze({
                "task_description": "fix main.py",
                "project_id": "proj-test",
            })

    assert len(result["knowledge_context"]["struct"]) == 1
    assert result["complexity"].value == "simple"
    print("  ✅ analyze — 接入知识检索")


async def _test_learn_persist_async():
    from swarm.brain.learn_store import (
        merge_persist_meta,
        persist_learn_failure,
        persist_learn_success,
    )
    from swarm.brain.nodes import learn_success

    from test.conftest import install_noop_transaction

    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.write_success = AsyncMock(return_value=42)
    mock_store.write_mistake = AsyncMock(return_value=99)
    mock_store.write_task_summary = AsyncMock()
    # P1-DEBT-03：落库前查相似已有记录决定强化 vs 插新；测试为新记录 → []。
    mock_store.query_successes = AsyncMock(return_value=[])
    mock_store.query_mistakes = AsyncMock(return_value=[])
    install_noop_transaction(mock_store)  # A-P1-26 事务上下文

    state = {
        "project_id": "proj-test",
        "task_id": "task-1",
        "task_description": "add sorting",
        "complexity": "medium",
        "plan": None,
        "merged_diff": "",
        "revision_feedback": "tests failed",
        # TD2606-A7：persist_learn_success 机制测试需真实成功状态（l2_passed），否则
        # should_write_success 正确拦下 L6 写入（failed_subtask_ids 会被判非成功）。
        "l2_passed": True,
    }

    with patch("swarm.brain.learn_store.MemoryStore", return_value=mock_store):
        meta = await persist_learn_success(state, {
            "pattern_name": "DTO排序模式",
            "pattern_description": "在 DTO 加 sortField",
            "applicable_scenarios": ["列表排序"],
        })
        assert meta["persisted"] is True
        assert meta["success_id"] == 42

        meta2 = await persist_learn_failure(state, {
            "mistake_description": "忘记默认值",
            "root_cause": "未读规范",
            "prevention_measures": ["检查 DTO 默认值"],
        })
        assert meta2["persisted"] is True
        assert meta2["mistake_id"] == 99

    merged = merge_persist_meta('{"pattern_name":"x"}', {"persisted": True, "success_id": 1})
    data = json.loads(merged)
    assert data["persist"]["success_id"] == 1

    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "pattern_name": "模式A",
            "pattern_description": "描述",
            "applicable_scenarios": ["场景"],
        })
        mock_llm.return_value.ainvoke = AsyncMock(return_value=mock_response)

        with patch("swarm.brain.learn_store.MemoryStore", return_value=mock_store):
            out = await learn_success({**state, "merged_diff": "diff"})

    assert out["learned"] is True
    summary = json.loads(out["learn_summary"])
    assert summary["persist"]["persisted"] is True
    print("  ✅ learn — 自动落库 L5/L6 + L2")


def test_analyze_retrieval():
    asyncio.run(_test_analyze_retrieval_async())


def test_learn_persist():
    asyncio.run(_test_learn_persist_async())


def main():
    print("\n🐝 Swarm 知识检索 & Learn 落库测试\n")
    print("=" * 50)

    tests = [
        test_format_layer_items,
        test_knowledge_tool_without_project,
        test_knowledge_tool_with_mock_retriever,
        test_analyze_retrieval,
        test_learn_persist,
    ]

    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("=" * 50)
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
