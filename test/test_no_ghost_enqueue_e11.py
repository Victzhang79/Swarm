"""E11（2026-07-09 深读登记册·阶段0）：直接执行路径不得自我 enqueue — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §六 E11：
  - run_task / resume_task / resume_planning 开跑时各有一处 TaskQueue.enqueue(自己)。
    这些函数本身【就是】执行入口（调度器 dequeue 后调用，或 API 直调）——把自己再入队
    只会制造幽灵队列项，调度器稍后 dequeue 到它们时任务已在跑/已终态，正确性全靠
    is_task_claimed 等三层去重兜底（scheduler.py:103 注释的误判窗正是这些幽灵造成）。
  - DB 是权威源（reconcile 会把 PENDING 重新入队），队列是派生缓存——删除自我 enqueue
    不丢任何工作信号。

测法：monkeypatch ModuleLock.acquire→False 使函数在 enqueue 点之后立即早退，
spy TaskQueue.enqueue 断言未被调用（不读源码，测可观测副作用）。
"""

from __future__ import annotations

import asyncio

import swarm.brain.runner as runner
import swarm.brain.scheduler as scheduler
from swarm.infra import redis_client


class _SpyQueue:
    calls: list = []

    @staticmethod
    def enqueue(task_id, project_id, priority="normal"):
        _SpyQueue.calls.append((task_id, project_id))


class _DenyLock:
    def __init__(self, *a, **k):
        pass

    def acquire(self):
        return False

    def release(self):
        pass


class _AllowLock:
    released = 0

    def __init__(self, *a, **k):
        pass

    def acquire(self):
        return True

    def release(self):
        type(self).released += 1


def _patch_common(monkeypatch):
    _SpyQueue.calls = []
    monkeypatch.setattr(redis_client.TaskQueue, "enqueue",
                        staticmethod(_SpyQueue.enqueue))
    monkeypatch.setattr(redis_client, "ModuleLock", _DenyLock)
    # runner 内部是函数内 from ... import —— redis_client 模块属性替换已覆盖


def test_run_task_does_not_self_enqueue(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(runner, "_set_workspace", lambda pid: None)
    runner._task_running.discard("t-e11")
    asyncio.run(runner.run_task("t-e11", "p-e11", "desc"))
    assert _SpyQueue.calls == [], (
        f"run_task 是执行入口，不得把自己再入队制造幽灵队列项: {_SpyQueue.calls}")
    assert "t-e11" not in runner._task_running  # 锁失败早退已清理


def test_run_task_rechecks_deleted_or_terminal_record_after_lock(monkeypatch):
    """出队后的取消/删除竞态必须在最终锁内重读处截止，不能创建 graph/沙箱。"""
    monkeypatch.setattr(runner, "_set_workspace", lambda _pid: None)
    monkeypatch.setattr(redis_client, "ModuleLock", _AllowLock)
    sandbox_mod = __import__("swarm.worker.sandbox", fromlist=["get_sandbox_manager"])
    manager = type("NoopSandboxManager", (), {"kill_by_task": lambda self, _tid: None})()
    monkeypatch.setattr(sandbox_mod, "get_sandbox_manager", lambda: manager)
    _AllowLock.released = 0

    for task_id, record in (
        ("t-deleted-after-dequeue", None),
        ("t-cancelled-after-dequeue", {"id": "t-cancelled-after-dequeue", "status": "CANCELLED"}),
    ):
        runner._task_running.discard(task_id)
        monkeypatch.setattr(runner.store, "get_task", lambda _tid, rec=record: rec)
        monkeypatch.setattr(
            runner.store,
            "get_project",
            lambda _pid: {"id": _pid, "status": "READY"},
        )
        monkeypatch.setattr(
            runner,
            "get_compiled_brain_graph",
            lambda: (_ for _ in ()).throw(AssertionError("非法任务不得构建 graph")),
        )
        monkeypatch.setattr(
            __import__("swarm.memory.profile", fromlist=["load_profile_prompts"]),
            "load_profile_prompts",
            lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("非法任务不得进入执行初始化")
            ),
        )

        asyncio.run(runner.run_task(task_id, "p-e11", "desc"))

        assert task_id not in runner._task_running

    assert _AllowLock.released == 2


def test_scheduler_and_runner_share_fail_closed_execution_epoch_validator(monkeypatch):
    """unknown/human/apply/future-version saga 不能被 resolve 或 drain 重新派发。"""
    from swarm.brain.execution_epoch import is_runnable_execution_epoch
    from swarm.project import store

    invalid_sagas = [
        {"version": 2, "kind": "execute_claim", "phase": "claimed", "saga_id": "e"},
        {"version": 1, "kind": "execute_claim", "phase": "future", "saga_id": "e"},
        {"version": 1, "kind": "retry_claim", "phase": "claimed", "saga_id": "e"},
        {"version": 1, "kind": "human_gate_claim", "phase": "claimed", "saga_id": "e"},
        {"version": 1, "kind": "apply_diff_resume", "phase": "applying", "saga_id": "e"},
        {"version": 1, "kind": "unknown", "phase": "claimed", "saga_id": "e"},
    ]
    records = [
        {"id": f"bad-{i}", "project_id": "p", "description": "x", "status": "SUBMITTED",
         "resume_saga": saga}
        for i, saga in enumerate(invalid_sagas)
    ]
    assert all(not is_runnable_execution_epoch(rec) for rec in records)
    assert is_runnable_execution_epoch({"status": "SUBMITTED", "resume_saga": {}})
    assert is_runnable_execution_epoch({
        "status": "SUBMITTED",
        "resume_saga": {
            "version": 1, "kind": "execute_claim", "phase": "claimed", "saga_id": "e",
        },
    })

    scheduler._pending_meta.clear()
    monkeypatch.setattr(store, "get_task", lambda _tid: records[0])
    assert scheduler._resolve_exec_meta(records[0]["id"]) is None

    enqueued: list[str] = []
    monkeypatch.setattr(store, "list_orphan_candidates", lambda: records)
    monkeypatch.setattr(
        scheduler.TaskQueue,
        "enqueue",
        lambda tid, *_a, **_kw: enqueued.append(tid),
    )
    assert asyncio.run(scheduler._drain_stranded_submitted()) == 0
    assert enqueued == []


def test_resume_task_does_not_self_enqueue(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(runner, "_set_workspace", lambda pid: None)
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "project_id": "p-e11", "status": "CONFIRMING"})
    _updates: list = []
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: _updates.append((tid, kw)))
    runner._task_running.discard("t-e11r")
    asyncio.run(runner.resume_task("t-e11r", "accept", revert_status="CONFIRMING"))
    assert _SpyQueue.calls == [], (
        f"resume_task 不得自我 enqueue: {_SpyQueue.calls}")
    assert "t-e11r" not in runner._task_running


def test_resume_planning_does_not_self_enqueue(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(runner, "_set_workspace", lambda pid: None)
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "project_id": "p-e11", "status": "CLARIFYING"})
    _updates: list = []
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: _updates.append((tid, kw)))
    runner._task_running.discard("t-e11p")
    asyncio.run(runner.resume_planning("t-e11p", "答复内容", revert_status="CLARIFYING"))
    assert _SpyQueue.calls == [], (
        f"resume_planning 不得自我 enqueue: {_SpyQueue.calls}")
    assert "t-e11p" not in runner._task_running
