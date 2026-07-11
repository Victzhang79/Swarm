"""B1：Redis 不可用时模块锁退【进程内锁】而非 fail-open no-op。

原 fail-open：Redis 挂/禁用时 acquire 恒 True → 同进程并发任务都"持锁" → 进程内双写。
改：退进程内锁——同 key 互斥(未持有者 False，调用方优雅延后)，不同 key 独立，且无争用即刻拿到
(不破坏 Redis 禁用的单进程模式)。行为测试 mock get_redis→None 触发回退。
"""
from __future__ import annotations

import uuid

import swarm.infra.redis_client as rc


def test_local_lock_mutual_exclusion_when_redis_down(monkeypatch):
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = f"_b1_{uuid.uuid4().hex[:8]}"
    a = rc.ModuleLock(pid, "mod")
    b = rc.ModuleLock(pid, "mod")
    assert a.acquire() is True, "无争用应即刻拿到"
    assert b.acquire() is False, "同 key 第二任务不应也拿到（原 fail-open 双持=进程内双写）"
    a.release()
    assert b.acquire() is True, "持有者释放后应可拿"
    b.release()


def test_local_lock_different_keys_independent(monkeypatch):
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = f"_b1_{uuid.uuid4().hex[:8]}"
    a = rc.ModuleLock(pid, "modA")
    b = rc.ModuleLock(pid, "modB")
    assert a.acquire() is True
    assert b.acquire() is True, "不同 key 应互不阻塞"
    a.release()
    b.release()


def test_release_idempotent_when_redis_down(monkeypatch):
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = f"_b1_{uuid.uuid4().hex[:8]}"
    a = rc.ModuleLock(pid, "mod")
    assert a.acquire() is True
    a.release()
    a.release()  # 二次 release 不应抛
    # 释放后同 key 新实例可再拿
    b = rc.ModuleLock(pid, "mod")
    assert b.acquire() is True
    b.release()


class _FakeRedis:
    """够用的假 Redis：SET NX 恒成功、Lua eval(释放/续期)返回 1。"""

    def set(self, *a, **k):
        return True

    def eval(self, *a, **k):
        return 1


def test_redis_acquired_also_holds_local_authoritative(monkeypatch):
    # H-2 治本（原 R1 复核场景已被结构性消除）：A 经 Redis 获取时【同时持进程级本地锁】作为
    # 权威互斥。故 Redis 宕机后 B 请求同 key 会被 A 的本地锁直接挡下（return False）——旧设计
    # 里"B 拿到进程内锁 + A 持 Redis"的双域窗口不复存在（更强的正确性）。
    pid = f"_b1_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(rc, "get_redis", lambda: _FakeRedis())
    a = rc.ModuleLock(pid, "mod")
    assert a.acquire() is True and a._local_held is True and a._redis_held is True

    monkeypatch.setattr(rc, "get_redis", lambda: None)  # Redis 宕机
    b = rc.ModuleLock(pid, "mod")
    assert b.acquire() is False, "A 持进程级权威锁 → Redis 宕机也挡住 B（无双域）"

    a.release()  # 释放两层：Redis 挂→key 靠 TTL 回收；进程级本地锁正常释放
    c = rc.ModuleLock(pid, "mod")
    assert c.acquire() is True, "A 释放后同 key 可再获取（未死锁）"
    c.release()


def test_local_acquired_release_when_redis_back_up(monkeypatch):
    # 对偶：A 经进程内锁获取(Redis 挂)；Redis 恢复后 release 必须释放【进程内锁】而非走 Redis 分支
    # （否则本地锁永不释放 → 该 key 死锁）。
    pid = f"_b1_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    a = rc.ModuleLock(pid, "mod")
    assert a.acquire() is True  # 进程内，_local_held=True

    monkeypatch.setattr(rc, "get_redis", lambda: _FakeRedis())  # Redis 恢复
    a.release()  # 必须释放进程内锁

    monkeypatch.setattr(rc, "get_redis", lambda: None)
    b = rc.ModuleLock(pid, "mod")
    assert b.acquire() is True, "进程内锁 release 后同 key 应可再获取（未死锁）"
    b.release()


def test_renew_noop_true_when_redis_down(monkeypatch):
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = f"_b1_{uuid.uuid4().hex[:8]}"
    a = rc.ModuleLock(pid, "mod")
    a.acquire()
    assert a.renew() is True, "进程内锁无 TTL，renew no-op True"
    a.release()
