#!/usr/bin/env python3
"""approve → Brain accept → LEARN 落库链路测试（mock resume / MemoryStore）"""

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


def test_approve_resumes_brain_accept():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {
        "id": "task-1",
        "project_id": "p1",
        "merged_diff": "",
        "status": "DELIVERING",
    }

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        mock_store.get_project.return_value = {"id": "p1", "path": "/tmp"}
        mock_store.update_task.return_value = task
        with patch("swarm.brain.runner.resume_task_background") as mock_resume:
            with patch("swarm.brain.runner.register_task_queue"):
                client = TestClient(app)
                resp = client.post("/api/tasks/task-1/approve")
                assert resp.status_code == 200, resp.text
                mock_resume.assert_called_once_with("task-1", "accept")
    print("  ✅ approve → resume_task_background(accept)")


async def _test_learn_after_accept_writes_memory_async():
    """模拟 accept 后 learn_success 节点写入 MemoryStore。"""
    from swarm.brain.learn_store import persist_learn_success
    from swarm.brain.nodes import learn_success

    from test.conftest import install_noop_transaction

    mock_store = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.close = AsyncMock()
    mock_store.write_success = AsyncMock(return_value=7)
    mock_store.write_task_summary = AsyncMock()
    # P1-DEBT-03：learn 落库前会查相似已有记录决定强化 vs 插新；新模式无前例 → []。
    mock_store.query_successes = AsyncMock(return_value=[])
    mock_store.query_mistakes = AsyncMock(return_value=[])
    install_noop_transaction(mock_store)  # A-P1-26 事务上下文

    state = {
        "project_id": "proj-1",
        "task_id": "task-1",
        "task_description": "add sorting",
        "complexity": "medium",
        "plan": None,
        "merged_diff": "diff content",
        "human_decision": "ACCEPT",
    }

    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "pattern_name": "排序模式",
            "pattern_description": "列表排序实现",
            "applicable_scenarios": ["CRUD 列表"],
        })
        mock_llm.return_value.ainvoke = AsyncMock(return_value=mock_response)

        with patch("swarm.brain.learn_store.MemoryStore", return_value=mock_store):
            out = await learn_success(state)

    assert out["learned"] is True
    mock_store.write_success.assert_awaited()
    summary = json.loads(out["learn_summary"])
    assert summary["persist"]["persisted"] is True
    assert summary["persist"]["success_id"] == 7

    # 直调 persist 走 mock store（与上半同源，避免触真实 PG；WS4 幂等键确定性会让
    # 真库上的同 task 重放被判重复，单测应隔离）。
    with patch("swarm.brain.learn_store.MemoryStore", return_value=mock_store):
        meta = await persist_learn_success(state, {
            "pattern_name": "x",
            "pattern_description": "y",
            "applicable_scenarios": [],
        })
    assert meta["persisted"] is True
    print("  ✅ accept → learn_success → MemoryStore")


def test_learn_after_accept_writes_memory():
    asyncio.run(_test_learn_after_accept_writes_memory_async())


def main() -> int:
    tests = [
        test_approve_resumes_brain_accept,
        test_learn_after_accept_writes_memory,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\n✅ 全部 {len(tests)} 项 learn chain 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
