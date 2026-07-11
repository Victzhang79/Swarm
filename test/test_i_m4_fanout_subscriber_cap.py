"""主题I M-4（外部深审 MEDIUM）：Brain fanout 订阅数只有软告警 → 无界连接压垮进程。

病根：subscribe() 达 _MAX_SUBS_PER_TASK 仅 logger.warning 不拒绝；连接总数无限、流端点无专门
连接上限 → 项目成员可对本 task（乃至多 task）无限开 SSE/WS 连接制造内存(N×队列容量)/socket/
每心跳周期鉴权压力。治：单 task 硬上限 + 全进程总硬上限，达标即抛 FanoutSubscriberLimitExceeded，
端点转 429/1013 关闭；unsubscribe 正确减全局计数。
"""
from __future__ import annotations

import pytest

import swarm.brain.runner as runner
from swarm.brain.runner import FanoutSubscriberLimitExceeded, _FanoutTopic


@pytest.fixture(autouse=True)
def _reset_global():
    runner._global_sub_count = 0
    yield
    runner._global_sub_count = 0


def test_m4_per_task_hard_cap_rejects(monkeypatch):
    monkeypatch.setattr(runner, "_MAX_SUBS_PER_TASK", 3)
    monkeypatch.setattr(runner, "_GLOBAL_MAX_SUBS", 1000)
    topic = _FanoutTopic()
    subs = [topic.subscribe() for _ in range(3)]  # 占满
    assert len(subs) == 3
    with pytest.raises(FanoutSubscriberLimitExceeded):
        topic.subscribe()  # 第 4 个必须被硬拒（旧行为=仅告警照连）
    # 退订一个 → 腾位可再订。
    topic.unsubscribe(subs[0])
    q = topic.subscribe()
    assert q is not None


def test_m4_unsubscribe_decrements_global_count(monkeypatch):
    monkeypatch.setattr(runner, "_MAX_SUBS_PER_TASK", 100)
    monkeypatch.setattr(runner, "_GLOBAL_MAX_SUBS", 1000)
    topic = _FanoutTopic()
    q1 = topic.subscribe()
    q2 = topic.subscribe()
    assert runner._global_sub_count == 2
    topic.unsubscribe(q1)
    assert runner._global_sub_count == 1
    topic.unsubscribe(q2)
    assert runner._global_sub_count == 0
    # 重复退订不把计数减到负。
    topic.unsubscribe(q1)
    assert runner._global_sub_count == 0


def test_m4_global_cap_across_tasks(monkeypatch):
    """多 task 各未达单 task 上限，但全进程总数达全局硬上限 → 拒绝。"""
    monkeypatch.setattr(runner, "_MAX_SUBS_PER_TASK", 100)
    monkeypatch.setattr(runner, "_GLOBAL_MAX_SUBS", 4)
    t1, t2 = _FanoutTopic(), _FanoutTopic()
    t1.subscribe(); t1.subscribe()
    t2.subscribe(); t2.subscribe()
    assert runner._global_sub_count == 4
    with pytest.raises(FanoutSubscriberLimitExceeded):
        t2.subscribe()  # 全局上限（跨 task 累积）触发
    with pytest.raises(FanoutSubscriberLimitExceeded):
        t1.subscribe()


def test_m4_history_replay_still_works_under_cap(monkeypatch):
    """硬上限不破坏 late 订阅者历史回放（未达限时行为不变）。"""
    monkeypatch.setattr(runner, "_MAX_SUBS_PER_TASK", 10)
    monkeypatch.setattr(runner, "_GLOBAL_MAX_SUBS", 100)
    topic = _FanoutTopic()
    topic.publish({"step": "log", "i": 1})
    topic.publish({"step": "complete", "i": 2})
    q = topic.subscribe()  # late 订阅者
    got = []
    while not q.empty():
        got.append(q.get_nowait())
    assert [e["i"] for e in got] == [1, 2], "回放历史保序不受硬上限影响"


if __name__ == "__main__":
    print("run via pytest")
