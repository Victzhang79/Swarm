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


def test_subscriber_hard_cap_rejects(monkeypatch):
    """M-4：订阅者数达硬上限 → 告警【且硬拒】（抛 FanoutSubscriberLimitExceeded，端点转 429/1013）。

    旧行为仅软告警照连 → 成员可无限开连接压内存/socket/周期鉴权；M-4 改硬上限。
    不用 caplog：全量套件里先跑的测试会触发 _configure_app_logging 把 swarm logger 设为不向 root
    传播，caplog（挂 root）就收不到 → 顺序相关 flaky。直接在 runner 的 logger 上挂 handler 断言。
    """
    import logging

    import pytest

    from swarm.brain import runner
    from swarm.brain.runner import FanoutSubscriberLimitExceeded

    monkeypatch.setattr(runner, "_MAX_SUBS_PER_TASK", 2)
    monkeypatch.setattr(runner, "_GLOBAL_MAX_SUBS", 1000)
    monkeypatch.setattr(runner, "_global_sub_count", 0)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    runner.logger.addHandler(handler)
    old_level = runner.logger.level
    runner.logger.setLevel(logging.WARNING)
    try:
        t = runner._FanoutTopic(history=0)
        t.subscribe()
        t.subscribe()  # 占满硬上限 2
        with pytest.raises(FanoutSubscriberLimitExceeded):
            t.subscribe()  # 第 3 个硬拒（旧行为=照连）
    finally:
        runner.logger.removeHandler(handler)
        runner.logger.setLevel(old_level)
    msgs = [r.getMessage() for r in records]
    assert any("订阅者" in m or "subscriber" in m.lower() for m in msgs), f"未捕获硬上限告警: {msgs}"


def test_late_subscriber_replays_most_recent_history(monkeypatch):
    """复核 F3：history 长于队列容量时，late 订阅者回放【最近 maxsize 条】而非最旧那批。"""
    from swarm.brain import runner

    monkeypatch.setattr(runner, "_SUB_QUEUE_MAXSIZE", 3)
    t = runner._FanoutTopic(history=100)
    for i in range(10):
        t.publish({"seq": i})
    q = t.subscribe()  # late 订阅者：容量 3 → 应拿最近 3 条 (7,8,9)
    got = []
    while not q.empty():
        got.append(q.get_nowait()["seq"])
    assert got == [7, 8, 9], f"应回放最新 3 条，实际 {got}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
