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
