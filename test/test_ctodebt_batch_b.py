"""SWARM_CTO_GUIDE Batch B 回归测试 — 并发/关闭正确性。

覆盖：N-CW1/N-CW2 SSE/WS fanout、P1-DEBT-13 coordination is_held 防脑裂、
N-09 scheduler stop、N-08/N-10 KB scheduler 取消、N-CW3 deque。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


# ── N-CW1/N-CW2：每订阅者独立队列 + 历史回放 + 注销 ──
def test_fanout_two_subscribers_each_get_all_events():
    from swarm.brain.runner import _FanoutTopic

    topic = _FanoutTopic()
    q1 = topic.subscribe()
    q2 = topic.subscribe()
    topic.publish({"step": "a"})
    topic.publish({"step": "b"})
    # 两个订阅者各自拿到全部 2 个事件（不再互相抢）
    assert [q1.get_nowait()["step"] for _ in range(2)] == ["a", "b"]
    assert [q2.get_nowait()["step"] for _ in range(2)] == ["a", "b"]


def test_fanout_late_subscriber_replays_history():
    from swarm.brain.runner import _FanoutTopic

    topic = _FanoutTopic()
    topic.publish({"step": "early"})
    q = topic.subscribe()  # 订阅发生在 publish 之后
    assert q.get_nowait()["step"] == "early"  # 仍能回放到先前事件


def test_fanout_unsubscribe_stops_delivery():
    from swarm.brain.runner import _FanoutTopic

    topic = _FanoutTopic()
    q = topic.subscribe()
    topic.unsubscribe(q)
    topic.publish({"step": "x"})
    assert q.empty()


def test_register_task_queue_idempotent_reuse():
    """N-CW1：重复 register 复用同一主题（retry/revise 不孤儿化在途订阅者）。"""
    import swarm.brain.runner as r

    t1 = r.register_task_queue("t_idem")
    q = t1.subscribe()
    t2 = r.register_task_queue("t_idem")  # retry 再注册
    assert t1 is t2, "重复 register 必须复用同一主题"
    t2.publish({"step": "after_retry"})
    assert q.get_nowait()["step"] == "after_retry"  # 在途订阅者仍收到事件
    r._task_queues.pop("t_idem", None)


# ── P1-DEBT-13：连接断开后 is_held 必须返回 False 并清空本地标记（防脑裂） ──
def test_coordination_is_held_false_when_conn_dropped():
    from swarm.infra.coordination import PgCoordinationBackend

    async def _run():
        be = PgCoordinationBackend("postgresql://x")
        be._held = {"leader-key"}
        be._conn = None  # 连接丢失
        assert await be.is_held("leader-key") is False
        assert be._held == set(), "连接断开后必须清空失效的本地持锁标记"

    asyncio.run(_run())


def test_coordination_is_held_false_when_conn_closed():
    from swarm.infra.coordination import PgCoordinationBackend

    async def _run():
        be = PgCoordinationBackend("postgresql://x")
        be._held = {"leader-key"}
        be._conn = MagicMock()
        be._conn.closed = True
        assert await be.is_held("leader-key") is False
        assert be._held == set()

    asyncio.run(_run())


def test_coordination_is_held_true_when_conn_alive():
    from swarm.infra.coordination import PgCoordinationBackend

    async def _run():
        be = PgCoordinationBackend("postgresql://x")
        be._held = {"leader-key"}
        be._conn = MagicMock()
        be._conn.closed = False
        assert await be.is_held("leader-key") is True

    asyncio.run(_run())


# ── N-09：task scheduler 可停止并重置状态 ──
def test_stop_task_scheduler_resets_state():
    import swarm.brain.scheduler as s

    async def _run():
        s._consumer_started = True
        s._consumer_task = None  # 无活跃 task 也应幂等清理
        await s.stop_task_scheduler()
        assert s._consumer_started is False
        assert s._consumer_task is None

    asyncio.run(_run())


# ── N-08/N-10：shutdown_kb_scheduler 取消后台轮询 task ──
def test_shutdown_kb_scheduler_cancels_poll_task():
    import swarm.knowledge.scheduler as kb

    async def _run():
        async def _forever():
            while True:
                await asyncio.sleep(3600)

        kb._poll_task = asyncio.create_task(_forever())
        await asyncio.sleep(0)  # 让 task 起跑
        await kb.shutdown_kb_scheduler()
        assert kb._poll_task is None
        assert kb._polling_started is False

    asyncio.run(_run())


# ── N-CW3：deque(maxlen) 有界，旧条目自动丢弃 ──
def test_sandbox_activity_deque_bounded():
    from collections import deque

    # 复刻 append_activity 的存储语义（deque(maxlen=500)）
    entries: deque = deque(maxlen=500)
    for i in range(600):
        entries.append({"i": i})
    assert len(entries) == 500
    assert entries[0]["i"] == 100  # 最旧 100 条已被丢弃
    assert entries[-1]["i"] == 599


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✅ {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ❌ {name}: {exc}")
    sys.exit(1 if failed else 0)
