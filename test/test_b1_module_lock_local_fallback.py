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


def test_renew_noop_true_when_redis_down(monkeypatch):
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    pid = f"_b1_{uuid.uuid4().hex[:8]}"
    a = rc.ModuleLock(pid, "mod")
    a.acquire()
    assert a.renew() is True, "进程内锁无 TTL，renew no-op True"
    a.release()
