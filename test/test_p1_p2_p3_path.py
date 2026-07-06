#!/usr/bin/env python3
"""P1-P3 路径测试"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import enrich_plan_with_shared_contract
from swarm.brain.graph import after_verify_l3
from swarm.brain.integration_review import check_contract_in_diff
from swarm.infra.redis_client import ModuleLock, TaskQueue, check_project_limit
from swarm.knowledge.retriever import SwarmRetriever
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def test_shared_contract_enrich():
    plan = TaskPlan(
        shared_contract={"interfaces": ["UserService"]},
        subtasks=[
            SubTask(
                id="a",
                description="d",
                difficulty=SubTaskDifficulty.MEDIUM,
                modality=SubTaskModality.TEXT,
                scope=FileScope(writable=["a.py"]),
                contract={"output": "impl"},
            )
        ],
        parallel_groups=[["a"]],
    )
    enriched = enrich_plan_with_shared_contract(plan)
    assert "interfaces" in enriched.subtasks[0].contract
    assert enriched.subtasks[0].contract["output"] == "impl"


def test_contract_symbols_in_diff():
    ok, issues = check_contract_in_diff(
        "--- a/x.py\n+++ b/x.py\n@@\n+class UserService:\n",
        {"interfaces": ["UserService"]},
    )
    assert ok, issues


def test_after_verify_l3_gate():
    assert after_verify_l3({"l3_skipped": True}) == "deliver"
    assert after_verify_l3({"l3_passed": False}) == "handle_failure"
    assert after_verify_l3({"l3_passed": True}) == "deliver"


# 批5：test_hybrid_fusion 已删——_apply_hybrid_fusion 为写者无读者（产出仅本测试消费，
# Brain/planner 不读），随生产代码一并移除。


def test_module_lock_memory_fallback():
    with patch("swarm.infra.redis_client.redis_enabled", return_value=False):
        lock = ModuleLock("p1", "mod")
        assert lock.acquire() is True
        lock.release()


def test_task_queue_memory():
    with patch("swarm.infra.redis_client.redis_enabled", return_value=False):
        TaskQueue._clear_memory()
        TaskQueue.enqueue("t1", "p1")
        item = TaskQueue.dequeue()
        assert item["task_id"] == "t1"
        TaskQueue._clear_memory()


def test_task_queue_priority_order():
    """优先级队列：urgent 先于 normal 先于 background 出队。"""
    with patch("swarm.infra.redis_client.redis_enabled", return_value=False):
        TaskQueue._clear_memory()
        # 按 normal → background → urgent 顺序入队
        TaskQueue.enqueue("t_normal", "p1")
        TaskQueue.enqueue("t_bg", "p1", priority="background")
        TaskQueue.enqueue("t_urgent", "p1", priority="urgent")
        # 出队顺序应为 urgent → normal → background
        item1 = TaskQueue.dequeue()
        assert item1["task_id"] == "t_urgent"
        assert item1["priority"] == "urgent"
        item2 = TaskQueue.dequeue()
        assert item2["task_id"] == "t_normal"
        assert item2["priority"] == "normal"
        item3 = TaskQueue.dequeue()
        assert item3["task_id"] == "t_bg"
        assert item3["priority"] == "background"
        # 队列空
        assert TaskQueue.dequeue() is None
        TaskQueue._clear_memory()


def test_task_queue_backward_compat():
    """向后兼容：不传 priority 时默认 normal。"""
    with patch("swarm.infra.redis_client.redis_enabled", return_value=False):
        TaskQueue._clear_memory()
        TaskQueue.enqueue("t1", "p1")
        item = TaskQueue.dequeue()
        assert item["task_id"] == "t1"
        assert item["priority"] == "normal"
        TaskQueue._clear_memory()


def test_check_project_limit_no_pg():
    """check_project_limit 在 PG 不可用时优雅降级。"""
    with patch("swarm.infra.redis_client.redis_enabled", return_value=False):
        result = check_project_limit()
        # PG 不可用时 active=-1, warn=False
        assert result["warn"] is False
        assert result["limit"] > 0


def test_scheduler_submit_enqueues_with_priority():
    """准入调度器 submit_task 入队并保留优先级，pending_count 跟踪。"""
    from swarm.brain import scheduler
    from swarm.infra.redis_client import TaskQueue

    with patch("swarm.infra.redis_client.redis_enabled", return_value=False):
        TaskQueue._clear_memory()
        scheduler._pending_meta.clear()
        scheduler._inflight.clear()

        scheduler.submit_task("t_norm", "p1", "普通任务")
        scheduler.submit_task("t_urg", "p1", "紧急任务", priority="urgent")
        # 两个任务都在队列，pending_count 反映积压
        assert scheduler.pending_count() == 2
        # urgent 先出队
        item = TaskQueue.dequeue()
        assert item["task_id"] == "t_urg" and item["priority"] == "urgent"
        TaskQueue._clear_memory()
        scheduler._pending_meta.clear()


if __name__ == "__main__":
    test_shared_contract_enrich()
    test_contract_symbols_in_diff()
    test_after_verify_l3_gate()
    test_module_lock_memory_fallback()
    test_task_queue_memory()
    test_task_queue_priority_order()
    test_task_queue_backward_compat()
    test_check_project_limit_no_pg()
    test_scheduler_submit_enqueues_with_priority()
    print("test_p1_p2_p3_path: all passed")
