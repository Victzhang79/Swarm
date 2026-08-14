#!/usr/bin/env python3
"""30 号文批12 E-1 锁：读者续期 Lua 加成员校验（ZSCORE 自查，fail-closed）。

原 `_PGATE_RENEW_SHARED_LUA` 只做「有写者返 0，否则无条件 ZADD 重登记返 1」——
读者位因续期中断 >TTL 被写者 acquire 的 ZREMRANGEBYSCORE 合法清掉后，写者来了又走，
旧读者 renew 撞见「无写者」→ 重登记返 1 → 失锁全程零信号（H-3 双写威胁的事后检测
缺一层）。治法=ZSCORE 自查：位不在（曾过期被清/从未登记）即返 0，与另两层状态权威
（key renew GET==token / writer renew hget==token）同构。

真 Redis 集成面（needs_service("redis")）跑【真 Lua 脚本】——删 ZSCORE 行的突变
必须让本文件红（fake 复刻语义逮不到脚本本身的漂移）。
"""
from __future__ import annotations

import time
import uuid

import pytest

pytestmark = pytest.mark.needs_service("redis")

import swarm.infra.redis_client as rc  # noqa: E402


@pytest.fixture()
def rconn():
    import redis

    from swarm.config.settings import get_config
    r = redis.from_url(get_config().db.redis_uri, decode_responses=True,
                       socket_connect_timeout=5, socket_timeout=5)
    yield r


@pytest.fixture()
def gate_keys():
    """一次性项目门的两个键；测试后清理（绝不留痕真实 Redis）。"""
    pid = f"e1-test-{uuid.uuid4().hex[:12]}"
    hk, zk = rc._pgate_key(pid), rc._pgate_readers_key(pid)
    yield hk, zk
    # 清理交由调用方经 rconn 做（fixture 不持连接）


def _cleanup(r, hk, zk):
    r.delete(hk)
    r.delete(zk)


def test_healthy_reader_renew_returns_1(rconn, gate_keys):
    """健康续期（读者位 score 新鲜）→ 返 1 且 score 被刷到未来——零误杀反向锁。"""
    hk, zk = gate_keys
    try:
        now = int(time.time())
        rconn.zadd(zk, {"reader-t1": now + 60})  # 位活着（未过期）
        ok = rconn.eval(rc._PGATE_RENEW_SHARED_LUA, 2, hk, zk, "reader-t1", 60)
        assert int(ok) == 1, "健康读者续期必须返 1（ZSCORE 自查不得误杀在场读者）"
        assert rconn.zscore(zk, "reader-t1") > now + 30, "续期必须真刷新 score"
    finally:
        _cleanup(rconn, hk, zk)


def test_expired_slot_then_writer_gone_renew_returns_0(rconn, gate_keys):
    """E-1 主场景：读者位过期 → 被写者 acquire 路径 ZREMRANGEBYSCORE 合法清掉 →
    写者来了又走 → 旧读者 renew 必须返 0（fail-closed 确认丢门），
    绝不无条件 ZADD 重登记（原病=返 1 零失锁信号）。"""
    hk, zk = gate_keys
    try:
        now = int(time.time())
        rconn.zadd(zk, {"reader-t1": now - 10})  # 位已过期（score 在过去）
        # 写者 acquire 路径的合法清理（与 _PGATE_ACQ_*_LUA 的 ZREMRANGEBYSCORE 同语义）
        rconn.zremrangebyscore(zk, "-inf", now)
        # 写者来了又走
        rconn.hset(hk, "w", "writer-x")
        rconn.hdel(hk, "w")
        ok = rconn.eval(rc._PGATE_RENEW_SHARED_LUA, 2, hk, zk, "reader-t1", 60)
        assert int(ok) == 0, (
            "读者位曾过期被清 ⇒ renew 必须返 0 fail-closed；返 1=失锁零信号（E-1 原病）")
        assert rconn.zscore(zk, "reader-t1") is None, "判丢门后不得重登记读者位"
    finally:
        _cleanup(rconn, hk, zk)


def test_never_registered_reader_renew_returns_0(rconn, gate_keys):
    """从未登记的 token 直接 renew（分叉态/乱序调用）→ 返 0，不得凭空调出一个读者位。"""
    hk, zk = gate_keys
    try:
        ok = rconn.eval(rc._PGATE_RENEW_SHARED_LUA, 2, hk, zk, "ghost-reader", 60)
        assert int(ok) == 0
        assert rconn.zscore(zk, "ghost-reader") is None
    finally:
        _cleanup(rconn, hk, zk)


def test_writer_present_still_returns_0(rconn, gate_keys):
    """回归：写者在场时读者 renew 照旧返 0（新旧判据在该场景同答，防误改第一档）。"""
    hk, zk = gate_keys
    try:
        rconn.zadd(zk, {"reader-t1": int(time.time()) + 60})
        rconn.hset(hk, "w", "writer-x")
        ok = rconn.eval(rc._PGATE_RENEW_SHARED_LUA, 2, hk, zk, "reader-t1", 60)
        assert int(ok) == 0
    finally:
        _cleanup(rconn, hk, zk)


def test_module_lock_renew_propagates_lost_gate(rconn, gate_keys, monkeypatch):
    """接线锁：Lua 返 0 ⇒ ModuleLock.renew() 必须返 False（fail-closed 上抛链），
    且 _gate_redis_held 落 False（不再续一把确认丢掉的门）。"""
    hk, zk = gate_keys
    try:
        rconn.zadd(zk, {"reader-t2": int(time.time()) - 10})  # 位已过期
        rconn.zremrangebyscore(zk, "-inf", int(time.time()))
        lock = rc.ModuleLock.__new__(rc.ModuleLock)
        lock._held = True
        lock._redis_held = False  # 分叉态：门持 key 未持（E-1 四联前提①同型）
        lock._gate_redis_held = True
        lock._is_project_wide = False
        lock.token = "reader-t2"
        lock.ttl_sec = 60
        lock.key = "k"
        lock._last_ok_monotonic = time.monotonic()
        lock._gate_last_ok_monotonic = time.monotonic()
        monkeypatch.setattr(rc, "get_redis", lambda: rconn)
        monkeypatch.setattr(rc, "_pgate_key", lambda _pid: hk)
        monkeypatch.setattr(rc, "_pgate_readers_key", lambda _pid: zk)
        lock.project_id = "whatever"
        assert lock.renew() is False, "门 Lua 确认丢门 ⇒ renew 必须 False（fail-closed）"
        assert lock._gate_redis_held is False
    finally:
        _cleanup(rconn, hk, zk)
