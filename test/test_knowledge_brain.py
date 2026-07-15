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


def test_g11_similarity_floor_filters_low_recall():
    """G11（Task#9 审计⑥）：错题/成功等相似度排名层，低于相似度地板的召回被丢弃
    （防异栈/陈旧错题误导大脑）；struct 层不设地板。"""
    import os
    from unittest.mock import patch

    from swarm.knowledge.service import format_layer_items
    mistakes = [
        {"error_type": "high", "fix_description": "相关错题", "similarity": 0.8},
        {"error_type": "low", "fix_description": "异栈噪声", "similarity": 0.05},
    ]
    with patch.dict(os.environ, {"SWARM_RECALL_SIMILARITY_FLOOR": "0.25"}):
        out = format_layer_items("mistakes", mistakes, 5)
    titles = [o["title"] for o in out]
    assert "high" in titles and "low" not in titles, f"低相似度错题必须被地板过滤: {titles}"


def test_g11_floor_fail_open_when_no_similarity():
    """fail-open：条目无相似度字段（嵌入不可用/降级）→ 全保留，绝不清空召回。"""
    import os
    from unittest.mock import patch

    from swarm.knowledge.service import format_layer_items
    mistakes = [{"error_type": "a", "fix_description": "x"},
                {"error_type": "b", "fix_description": "y"}]
    with patch.dict(os.environ, {"SWARM_RECALL_SIMILARITY_FLOOR": "0.5"}):
        out = format_layer_items("mistakes", mistakes, 5)
    assert len(out) == 2, "无相似度字段的条目 fail-open 全保留"


def test_g11_struct_layer_not_floored():
    """struct 层（精确符号查，非相似度排名）不受地板影响。"""
    import os
    from unittest.mock import patch

    from swarm.knowledge.service import format_layer_items
    items = [{"symbol_name": "foo", "file_path": "a.py", "signature": "def foo",
              "similarity": 0.01}]
    with patch.dict(os.environ, {"SWARM_RECALL_SIMILARITY_FLOOR": "0.5"}):
        out = format_layer_items("struct", items, 5)
    assert len(out) == 1, "struct 层不设相似度地板"


def test_g11_semantic_layer_not_floored_bm25_scale_safe():
    """复核 CRITICAL#2 整改：semantic 层【不设地板】——嵌入宕机降级为 BM25(异 scale)，
    拿余弦地板卡会静默清空关键词兜底召回（N-12/N-13）。低 score 的 semantic 必须保留。"""
    import os
    from unittest.mock import patch

    from swarm.knowledge.service import format_layer_items
    items = [{"file_path": "a.py", "content": "kw hit", "score": 0.03}]
    with patch.dict(os.environ, {"SWARM_RECALL_SIMILARITY_FLOOR": "0.5"}):
        out = format_layer_items("semantic", items, 5)
    assert len(out) == 1, "semantic 层降级 BM25 scale，绝不设余弦地板"


def test_g11_zero_similarity_fail_open():
    """复核 CRITICAL#1 整改：0.0 是 store.py 对【空 embedding 行】NULL 相似度的 coerce 值 →
    必须 fail-open 保留（无向量 pinned/降级行不被误清），只丢真·(0,floor) 弱相关。"""
    import os
    from unittest.mock import patch

    from swarm.knowledge.service import format_layer_items
    mistakes = [{"error_type": "pinned", "fix_description": "无向量行", "similarity": 0.0},
                {"error_type": "weak", "fix_description": "弱相关噪声", "similarity": 0.1}]
    with patch.dict(os.environ, {"SWARM_RECALL_SIMILARITY_FLOOR": "0.25"}):
        out = format_layer_items("mistakes", mistakes, 5)
    titles = [o["title"] for o in out]
    assert "pinned" in titles, "0.0（空向量 coerce）必须 fail-open 保留"
    assert "weak" not in titles, "真·(0,floor) 弱相关仍被过滤"


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
