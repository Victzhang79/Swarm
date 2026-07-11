"""主题I M-1（外部深审 MEDIUM）：TaskQueue 的 Redis fallback 实际不可达。

病根：enqueue 的 RPush / dequeue 的 LPop 无异常处理——缓存 client 连上后故障时抛异常
外泄（submit_task/消费循环之上无兜底）→ 任务入队/出队直接崩，从不落内存 fallback；且
停机期堆积的内存条目在 Redis 恢复后永久滞留不可达（后续都走 Redis）。
治：enqueue/dequeue 的 Redis 路径包 try→异常作废坏 client(_invalidate_redis)+转内存兜底；
Redis 路径开头 _drain_memory_to_redis 把内存残留冲回 Redis（保优先级+FIFO），双源合一。
"""
from __future__ import annotations

import json

import swarm.infra.redis_client as rc


class _FakeRedis:
    def __init__(self):
        self.lists: dict = {}

    def rpush(self, key, val):
        self.lists.setdefault(key, []).append(val)

    def lpop(self, key):
        lst = self.lists.get(key) or []
        return lst.pop(0) if lst else None

    def blpop(self, keys, timeout=1):
        for k in keys:
            lst = self.lists.get(k) or []
            if lst:
                return (k, lst.pop(0))
        return None


class _BrokenRedis:
    def rpush(self, *a, **k):
        raise ConnectionError("cached client went bad")

    def lpop(self, *a, **k):
        raise ConnectionError("cached client went bad")

    def blpop(self, *a, **k):
        raise ConnectionError("cached client went bad")


def _payload(tid, pri="normal"):
    return json.dumps({"task_id": tid, "project_id": "p", "priority": pri})


def test_m1_enqueue_broken_client_falls_to_memory(monkeypatch):
    rc.TaskQueue._clear_memory()
    monkeypatch.setattr(rc, "get_redis", lambda: _BrokenRedis())
    rc.TaskQueue.enqueue("t1", "p", "normal")  # 绝不抛
    assert any(rc.TaskQueue._memory[p] for p in rc.TaskQueue._PRIORITIES), "坏 client 时落内存兜底"
    rc.TaskQueue._clear_memory()


def test_m1_dequeue_broken_client_falls_to_memory(monkeypatch):
    rc.TaskQueue._clear_memory()
    rc.TaskQueue._memory["normal"].append(_payload("t1"))
    monkeypatch.setattr(rc, "get_redis", lambda: _BrokenRedis())
    got = rc.TaskQueue.dequeue()  # 绝不抛，从内存出
    assert got and got["task_id"] == "t1"
    rc.TaskQueue._clear_memory()


def test_m1_memory_drained_to_redis_on_recovery_via_enqueue(monkeypatch):
    rc.TaskQueue._clear_memory()
    monkeypatch.setattr(rc, "get_redis", lambda: None)  # 停机
    rc.TaskQueue.enqueue("t1", "p", "urgent")
    assert rc.TaskQueue._memory["urgent"]
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)  # 恢复
    rc.TaskQueue.enqueue("t2", "p", "urgent")
    assert not rc.TaskQueue._memory["urgent"], "内存残留必须冲进 Redis（不永久滞留）"
    assert rc.TaskQueue.dequeue()["task_id"] == "t1", "保序：停机期 t1 先于恢复后 t2"
    assert rc.TaskQueue.dequeue()["task_id"] == "t2"
    rc.TaskQueue._clear_memory()


def test_m1_dequeue_drains_memory_on_recovery(monkeypatch):
    rc.TaskQueue._clear_memory()
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    rc.TaskQueue.enqueue("t1", "p", "normal")
    fake = _FakeRedis()
    monkeypatch.setattr(rc, "get_redis", lambda: fake)
    got = rc.TaskQueue.dequeue()  # 先冲内存进 Redis 再出 → t1（不滞留）
    assert got["task_id"] == "t1"
    assert not rc.TaskQueue._memory["normal"]
    rc.TaskQueue._clear_memory()


def test_m1_blocking_broken_client_never_raises(monkeypatch):
    """dequeue_blocking BLPOP 抛 → 作废坏 client + 回退非阻塞 → 一路兜到内存，绝不外泄。"""
    rc.TaskQueue._clear_memory()
    rc.TaskQueue._memory["normal"].append(_payload("t1"))
    monkeypatch.setattr(rc, "get_redis", lambda: _BrokenRedis())
    got = rc.TaskQueue.dequeue_blocking(1.0)
    assert got and got["task_id"] == "t1", "坏 client 时经内存兜底出队，不崩消费循环"
    rc.TaskQueue._clear_memory()


if __name__ == "__main__":
    print("run via pytest")
