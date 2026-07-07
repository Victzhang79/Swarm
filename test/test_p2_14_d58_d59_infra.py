"""P2-14 D58 / D59 行为测试。

D58：任务队列 BLPOP 事件化（多 key 一次往返、可中断、失败回退非阻塞）；
     准入闸门按任务 next-retry（不再全局 sleep(3.0) 造成队头阻塞）。
D59：装饰性配置删除、TaskStatus 与 task_states SSOT 对齐、_NOTIFY_STATUSES 补两终态。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest


# ─── D58a：BLPOP 阻塞式出队 ────────────────────────────


class _FakeRedis:
    def __init__(self, items=None, boom=False):
        self.items = list(items or [])
        self.boom = boom
        self.blpop_calls: list = []
        self.lpop_calls: list = []

    def blpop(self, keys, timeout=0):
        self.blpop_calls.append((list(keys), timeout))
        if self.boom:
            raise ConnectionError("redis down")
        if self.items:
            key, val = self.items.pop(0)
            return key, val
        return None

    def lpop(self, key):
        self.lpop_calls.append(key)
        for i, (k, v) in enumerate(self.items):
            if k == key:
                self.items.pop(i)
                return v
        return None


def test_d58_dequeue_blocking_single_roundtrip_priority_order(monkeypatch):
    from swarm.infra import redis_client as rc

    payload = json.dumps({"task_id": "t1", "project_id": "p1", "priority": "normal"})
    fake = _FakeRedis(items=[("swarm:task_queue:normal", payload)])
    monkeypatch.setattr(rc, "get_redis", lambda: fake)

    item = rc.TaskQueue.dequeue_blocking(2.0)
    assert item == {"task_id": "t1", "project_id": "p1", "priority": "normal"}
    assert len(fake.blpop_calls) == 1                     # 一次往返（旧路径 = 3 个 LPOP）
    keys, timeout = fake.blpop_calls[0]
    assert keys == ["swarm:task_queue:urgent", "swarm:task_queue:normal",
                    "swarm:task_queue:background"]        # BLPOP 按 key 顺序 = 优先级顺序
    assert 1 <= timeout <= 2                              # 小超时保循环可中断


def test_d58_dequeue_blocking_falls_back_on_redis_error(monkeypatch):
    from swarm.infra import redis_client as rc

    payload = json.dumps({"task_id": "t2", "project_id": "p1", "priority": "urgent"})
    fake = _FakeRedis(items=[("swarm:task_queue:urgent", payload)], boom=True)
    monkeypatch.setattr(rc, "get_redis", lambda: fake)

    item = rc.TaskQueue.dequeue_blocking(2.0)
    assert item and item["task_id"] == "t2"               # 回退非阻塞 LPOP 拿到同一数据
    assert fake.lpop_calls, "BLPOP 异常必须回退原逐 key LPOP"


def test_d58_supports_blocking_memory_mode(monkeypatch):
    from swarm.infra import redis_client as rc

    monkeypatch.setattr(rc, "get_redis", lambda: None)
    assert rc.TaskQueue.supports_blocking() is False
    rc.TaskQueue._clear_memory()
    rc.TaskQueue.enqueue("tm", "pm")
    assert rc.TaskQueue.dequeue_blocking(2.0)["task_id"] == "tm"  # 内存模式=原非阻塞语义
    rc.TaskQueue._clear_memory()


# ─── D58b：准入等待不再队头阻塞 ─────────────────────────


@pytest.mark.timeout(20)
def test_d58_not_ready_project_does_not_block_ready_task(monkeypatch):
    """队头任务项目未就绪时，后队就绪任务须立刻派发（改前全局 sleep(3.0) 卡整条队列）。"""
    from swarm.brain import scheduler as sch
    from swarm.infra.redis_client import TaskQueue

    TaskQueue._clear_memory()
    dispatched: list[tuple[str, float]] = []

    monkeypatch.setattr(sch, "_is_already_running", lambda tid: False)
    monkeypatch.setattr(sch, "_resolve_exec_meta",
                        lambda tid: {"project_id": f"proj-{tid}", "description": "d",
                                     "auto_accept": False})
    monkeypatch.setattr(sch, "_project_ready_for_exec",
                        lambda pid: pid != "proj-waiting")
    monkeypatch.setattr(sch, "_run_with_slot",
                        lambda tid, meta, fn: dispatched.append((tid, time.monotonic())))

    async def _drain_noop():
        return None

    monkeypatch.setattr(sch, "_maybe_drain_stranded", _drain_noop)
    # 干净的调度器全局态
    sch._consumer_started = False
    sch._consumer_task = None
    sch._pending_meta.clear()
    sch._inflight.clear()
    sch._admission_retries.clear()
    sch._admission_next_retry.clear()
    sch._deferred_cycle.clear()

    async def main():
        t0 = time.monotonic()
        # 队头 = 未就绪项目的任务；队尾 = 就绪任务
        TaskQueue.enqueue("waiting", "proj-waiting")
        TaskQueue.enqueue("ready", "proj-ready")
        sch._pending_meta["waiting"] = {"project_id": "proj-waiting", "description": "", "auto_accept": False}
        sch._pending_meta["ready"] = {"project_id": "proj-ready", "description": "", "auto_accept": False}
        await sch.start_task_scheduler()
        try:
            while not dispatched and time.monotonic() - t0 < 5.0:
                await asyncio.sleep(0.05)
        finally:
            await sch.stop_task_scheduler()
        return t0

    t0 = asyncio.run(main())
    TaskQueue._clear_memory()
    assert dispatched and dispatched[0][0] == "ready"
    elapsed = dispatched[0][1] - t0
    # 改前：先出队 waiting → 全局 sleep(3.0) → ready 至少 3s 后才派发。
    assert elapsed < 2.0, f"就绪任务被未就绪队头阻塞 {elapsed:.2f}s"
    # waiting 留池且记了 next-retry（节奏 ≥3s 的就绪检查不变）
    assert sch._admission_retries.get("waiting") == 1
    assert "waiting" in sch._admission_next_retry


# ─── D59 ───────────────────────────────────────────────


def test_d59_decorative_config_fields_removed():
    from swarm.config.settings import KnowledgeConfig, WorkerConfig

    # 定义即终点的装饰性字段已删除（e2b/CubeMaster create 面不支持每沙箱资源上限）
    assert "memory_limit" not in WorkerConfig.model_fields
    assert "disk_limit" not in WorkerConfig.model_fields
    assert "index_update_timeout" not in KnowledgeConfig.model_fields
    # env 中残留 SWARM_WORKER_MEMORY_LIMIT 不炸（extra=ignore）
    import os
    old = os.environ.get("SWARM_WORKER_MEMORY_LIMIT")
    os.environ["SWARM_WORKER_MEMORY_LIMIT"] = "2g"
    try:
        WorkerConfig()
    finally:
        if old is None:
            os.environ.pop("SWARM_WORKER_MEMORY_LIMIT", None)
        else:
            os.environ["SWARM_WORKER_MEMORY_LIMIT"] = old


def test_d59_task_status_covers_task_states_ssot():
    from swarm.task_states import ACTIVE_DB_STATUSES, TERMINAL_STATES
    from swarm.types import TaskStatus

    values = {m.value for m in TaskStatus}
    missing = (set(ACTIVE_DB_STATUSES) | set(TERMINAL_STATES)) - values
    assert not missing, f"TaskStatus 缺 SSOT 状态: {missing}"
    assert "POOLED" in values                     # 需求池态也补全
    # 终态判定不受新增成员影响
    assert TaskStatus.is_terminal_status("PARTIAL")
    assert not TaskStatus.is_terminal_status("CLARIFYING")
    assert not TaskStatus.is_terminal_status("VERIFYING_L3")


def test_d59_notify_statuses_include_partial_and_cancelled():
    from swarm.project.store import _NOTIFY_STATUSES, _task_event_type

    assert "PARTIAL" in _NOTIFY_STATUSES
    assert "CANCELLED" in _NOTIFY_STATUSES
    assert "DONE" in _NOTIFY_STATUSES and "FAILED" in _NOTIFY_STATUSES
    assert "CLARIFYING" in _NOTIFY_STATUSES       # 既有中断挂起态不回退
    assert _task_event_type("PARTIAL") == "task_partial"
    assert _task_event_type("CANCELLED") == "task_cancelled"
    assert _task_event_type("DONE") == "task_completed"   # 既有映射不变
