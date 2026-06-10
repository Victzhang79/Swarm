#!/usr/bin/env python3
"""brain/scheduler.py + infra/redis_client.py 真实单测。

不依赖 Redis（用内存 fallback）、不依赖 DB（check_project_limit mock store）。
覆盖：优先级队列出队顺序、未知优先级降级、模块锁内存放行、
plan→module_key 推断、并发上限 env 覆盖、submit_task 入队 + pending_count。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.infra import redis_client
from swarm.infra.redis_client import (
    ModuleLock,
    TaskQueue,
    check_project_limit,
    get_max_active_projects,
    module_key_from_plan,
)


def _reset_queue():
    TaskQueue._clear_memory()


# ── TaskQueue 优先级 ─────────────────────────────


def test_task_queue_priority_order():
    """urgent → normal → background 严格优先级出队。"""
    _reset_queue()
    # 故意乱序入队
    TaskQueue.enqueue("t-bg", "p1", priority="background")
    TaskQueue.enqueue("t-normal", "p1", priority="normal")
    TaskQueue.enqueue("t-urgent", "p1", priority="urgent")

    assert TaskQueue.dequeue()["task_id"] == "t-urgent"
    assert TaskQueue.dequeue()["task_id"] == "t-normal"
    assert TaskQueue.dequeue()["task_id"] == "t-bg"
    assert TaskQueue.dequeue() is None
    print("  ✅ TaskQueue urgent>normal>background 出队顺序")


def test_task_queue_fifo_within_priority():
    """同优先级内 FIFO。"""
    _reset_queue()
    TaskQueue.enqueue("a", "p1", priority="normal")
    TaskQueue.enqueue("b", "p1", priority="normal")
    TaskQueue.enqueue("c", "p1", priority="normal")
    assert [TaskQueue.dequeue()["task_id"] for _ in range(3)] == ["a", "b", "c"]
    print("  ✅ TaskQueue 同优先级 FIFO")


def test_task_queue_unknown_priority_downgrades_to_normal():
    _reset_queue()
    TaskQueue.enqueue("x", "p1", priority="超急")  # 非法优先级
    TaskQueue.enqueue("y", "p1", priority="urgent")
    # 非法优先级降级为 normal，urgent 仍应先出
    assert TaskQueue.dequeue()["task_id"] == "y"
    assert TaskQueue.dequeue()["task_id"] == "x"
    print("  ✅ TaskQueue 未知优先级降级 normal")


def test_task_queue_default_priority_normal():
    _reset_queue()
    TaskQueue.enqueue("d", "p1")  # 不传 priority
    item = TaskQueue.dequeue()
    assert item["task_id"] == "d"
    assert item["priority"] == "normal"
    print("  ✅ TaskQueue 默认优先级 normal")


def test_task_queue_empty_returns_none():
    _reset_queue()
    assert TaskQueue.dequeue() is None
    print("  ✅ TaskQueue 空队列返回 None")


# ── ModuleLock 内存 fallback ─────────────────────


def test_module_lock_memory_fallback_grants():
    """Redis 不可用时锁直接放行（不阻塞单进程执行）。"""
    with patch.object(redis_client, "get_redis", return_value=None):
        lock = ModuleLock("proj-1", "api", ttl_sec=60)
        assert lock.acquire() is True
        assert lock._held is True
        lock.release()  # 不应抛异常
        assert lock._held is False
    print("  ✅ ModuleLock 内存 fallback 放行")


# ── module_key_from_plan 推断 ────────────────────


def test_module_key_from_plan_top_dir():
    plan = {"subtasks": [{"scope": {"writable": ["api/routers/task.py"]}}]}
    assert module_key_from_plan(plan) == "api"
    print("  ✅ module_key_from_plan 取顶层目录")


def test_module_key_from_plan_root_file():
    plan = {"subtasks": [{"scope": {"writable": ["setup.py"]}}]}
    assert module_key_from_plan(plan) == "root"
    print("  ✅ module_key_from_plan 根文件→root")


def test_module_key_from_plan_empty():
    assert module_key_from_plan(None) == "default"
    assert module_key_from_plan({"subtasks": []}) == "default"
    assert module_key_from_plan({"subtasks": [{"scope": {"writable": []}}]}) == "default"
    print("  ✅ module_key_from_plan 空→default")


def test_module_key_from_plan_windows_path():
    plan = {"subtasks": [{"scope": {"writable": ["worker\\sandbox.py"]}}]}
    assert module_key_from_plan(plan) == "worker"
    print("  ✅ module_key_from_plan 兼容反斜杠路径")


# ── 项目数软限制 ─────────────────────────────────


def test_get_max_active_projects_default(monkeypatch):
    # 重置缓存
    redis_client._SWARM_MAX_ACTIVE_PROJECTS = None
    monkeypatch.delenv("SWARM_MAX_ACTIVE_PROJECTS", raising=False)
    assert get_max_active_projects() == 10
    redis_client._SWARM_MAX_ACTIVE_PROJECTS = None
    print("  ✅ get_max_active_projects 默认 10")


def test_check_project_limit_warns_at_limit(monkeypatch):
    redis_client._SWARM_MAX_ACTIVE_PROJECTS = None
    monkeypatch.setenv("SWARM_MAX_ACTIVE_PROJECTS", "2")
    fake = [
        {"status": "INDEXED"},
        {"status": "PREPROCESSING"},
        {"status": "EMPTY"},  # 不计入活跃
    ]
    with patch("swarm.project.store.list_projects", return_value=fake):
        res = check_project_limit()
    assert res["active"] == 2
    assert res["limit"] == 2
    assert res["warn"] is True
    redis_client._SWARM_MAX_ACTIVE_PROJECTS = None
    print("  ✅ check_project_limit 达限告警(EMPTY 不计活跃)")


def test_check_project_limit_db_unavailable(monkeypatch):
    redis_client._SWARM_MAX_ACTIVE_PROJECTS = None
    monkeypatch.setenv("SWARM_MAX_ACTIVE_PROJECTS", "10")
    with patch("swarm.project.store.list_projects", side_effect=RuntimeError("no pg")):
        res = check_project_limit()
    assert res["active"] == -1
    assert res["warn"] is False
    redis_client._SWARM_MAX_ACTIVE_PROJECTS = None
    print("  ✅ check_project_limit PG 不可用降级")


# ── scheduler submit / pending ───────────────────


def test_scheduler_submit_enqueues_and_tracks_meta():
    from swarm.brain import scheduler

    _reset_queue()
    scheduler._pending_meta.clear()
    scheduler._inflight.clear()

    scheduler.submit_task("task-1", "proj-1", "修复 bug", auto_accept=True, priority="urgent")
    # 元数据被缓存
    assert "task-1" in scheduler._pending_meta
    assert scheduler._pending_meta["task-1"]["auto_accept"] is True
    # 入队成功（urgent 队列）
    item = TaskQueue.dequeue()
    assert item["task_id"] == "task-1"
    assert item["priority"] == "urgent"
    # pending_count = 缓存 meta + inflight
    assert scheduler.pending_count() == 1
    scheduler._pending_meta.clear()
    print("  ✅ scheduler.submit_task 入队 + 缓存元数据 + pending_count")


def test_scheduler_max_concurrent_env_override(monkeypatch):
    from swarm.brain import scheduler

    monkeypatch.setenv("SWARM_MAX_CONCURRENT_TASKS", "7")
    assert scheduler._max_concurrent() == 7
    # 非法值回退 config
    monkeypatch.setenv("SWARM_MAX_CONCURRENT_TASKS", "abc")
    assert scheduler._max_concurrent() >= 1
    print("  ✅ scheduler._max_concurrent env 覆盖 + 非法值回退")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                import inspect

                sig = inspect.signature(fn)
                if sig.parameters:
                    continue  # 跳过需要 fixture 的
                fn()
            except Exception as e:
                print(f"  ❌ {name}: {e}")
                raise
    print("\nscheduler/redis_client 单测通过。")
