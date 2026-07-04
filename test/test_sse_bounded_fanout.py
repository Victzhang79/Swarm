#!/usr/bin/env python3
"""P2-F 回归：SSE fanout 订阅者队列有界 + drop-oldest，防慢消费者 OOM。"""

from __future__ import annotations

import asyncio


def test_subscriber_queue_is_bounded():
    from swarm.brain.runner import _FanoutTopic

    t = _FanoutTopic(history=10)
    q = t.subscribe()
    assert q.maxsize > 0, "订阅者队列必须有界（P2-F）"


def test_publish_drops_oldest_on_full_not_unbounded():
    """慢消费者不取 → 队列满后 publish 丢最旧保最新，队列不越界增长。"""
    from swarm.brain import runner

    t = runner._FanoutTopic(history=0)
    q = t.subscribe()
    cap = q.maxsize
    # 生产 3×容量，消费者一直不取
    for i in range(cap * 3):
        t.publish({"seq": i})
    assert q.qsize() <= cap, f"队列越界：{q.qsize()} > {cap}（P2-F 回归）"
    # drop-oldest：队列里应是最后 cap 个事件（最新进度保留）
    seqs = []
    while not q.empty():
        seqs.append(q.get_nowait()["seq"])
    assert seqs[-1] == cap * 3 - 1, "最新事件必须保留"
    assert seqs == sorted(seqs), "drop-oldest 应保持单调新序"


def test_slow_subscriber_does_not_block_others():
    """一个满队列订阅者不影响其它订阅者继续收到最新事件。"""
    from swarm.brain import runner

    t = runner._FanoutTopic(history=0)
    slow = t.subscribe()
    fast = t.subscribe()
    for i in range(slow.maxsize + 5):
        t.publish({"seq": i})
        if not fast.empty():
            fast.get_nowait()  # fast 持续消费
    # slow 被 drop-oldest 保护未 OOM；fast 仍在收
    assert slow.qsize() <= slow.maxsize


def test_subscriber_soft_cap_warns(monkeypatch, caplog):
    """订阅者数超软上限 → 告警（可观测），但不硬拒（SSE 仍可连）。"""
    import logging
    from swarm.brain import runner

    monkeypatch.setattr(runner, "_MAX_SUBS_PER_TASK", 2)
    t = runner._FanoutTopic(history=0)
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            t.subscribe()
    assert any("订阅者" in r.message or "subscriber" in r.message.lower() for r in caplog.records)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
