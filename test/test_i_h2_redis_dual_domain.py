"""主题I H-2（外部深审 HIGH）：Redis 恢复期本地锁与 Redis 锁双持（两个互不相识的锁域）。

病根：Redis 宕机窗口 acquire 退 _local_lock_for（进程内 threading.Lock）；冷却重探后 Redis
恢复，新请求获取同 key 走 Redis SET NX——Redis 对本地 holder 零记录 → SET 成功 → 同一 key
被"两个域"同时持有 = 双写（模块锁本要防的正是这个）。
治：Redis 可用的 acquire 路径先探 _local_lock_for(key).locked()——被同进程 fallback holder
持有则让位（return False），等其 release 后 Redis 路径再接管。正常 Redis-up 期本地锁从不被持
（Redis 路径 _local_held 恒 False），故 .locked()==True ⟺ 存在 fallback holder。
"""
from __future__ import annotations

import uuid

import swarm.infra.redis_client as rc


class _OkRedis:
    """Redis 恢复态：SET NX 对本地 holder 无记录 → 会"成功"（正是双域根源）。"""
    def set(self, *a, **k):
        return True

    def eval(self, *a, **k):
        return 1


def test_h2_redis_recovery_yields_to_local_holder(monkeypatch):
    pid = f"_h2_{uuid.uuid4().hex[:8]}"
    # ① Redis 宕机 → A 取本地 fallback 锁
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    a = rc.ModuleLock(pid, "mod")
    assert a.acquire() is True and a._local_held, "Redis down → 本地 fallback 持有"

    # ② Redis 恢复 → B 请求同 key 走 Redis：必须让位（不造双域），而非 SET NX 成功双持
    monkeypatch.setattr(rc, "get_redis", lambda: _OkRedis())
    b = rc.ModuleLock(pid, "mod")
    assert b.acquire() is False, "H-2：Redis 恢复期须尊重同进程 fallback holder，让位"
    assert b._held is False

    # ③ A 释放本地锁后，Redis 路径接管放行
    a.release()
    assert b.acquire() is True, "fallback holder 释放后 Redis 路径正常接管"
    b.release()


def test_h2_redis_acquire_holds_both_layers(monkeypatch):
    """治本后：Redis 路径【也】持进程级本地锁（权威互斥层）+ Redis 跨进程层。"""
    pid = f"_h2n_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(rc, "get_redis", lambda: _OkRedis())
    lock = rc.ModuleLock(pid, "mod")
    assert lock.acquire() is True
    assert lock._held and lock._local_held is True and lock._redis_held is True
    lock.release()
    # release 后两层都放（本地锁不再 locked）
    assert rc._local_lock_for(lock.key).locked() is False


def test_h2_same_process_same_key_second_acquire_blocked(monkeypatch):
    """同进程同 key 第二个 acquire 被本地权威锁挡住（不再依赖 Redis NX 兜底）。"""
    pid = f"_h2s_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(rc, "get_redis", lambda: _OkRedis())
    a = rc.ModuleLock(pid, "mod")
    b = rc.ModuleLock(pid, "mod")
    assert a.acquire() is True
    assert b.acquire() is False, "第二个同进程同 key 直接被进程级锁挡下"
    a.release()
    assert b.acquire() is True
    b.release()


def test_h2_redis_taken_by_other_process_releases_local(monkeypatch):
    """他进程经 Redis 持有（SET NX 返回 None）→ 本地锁必须回退不留孤儿。"""
    pid = f"_h2o_{uuid.uuid4().hex[:8]}"

    class _RedisKeyTaken:
        def set(self, *a, **k):
            return None  # NX 失败=他进程已持有

    monkeypatch.setattr(rc, "get_redis", lambda: _RedisKeyTaken())
    lock = rc.ModuleLock(pid, "mod")
    assert lock.acquire() is False
    assert lock._held is False and lock._local_held is False
    assert rc._local_lock_for(lock.key).locked() is False, "让位时本地锁必须已释放（无孤儿）"


def test_h2_reentrant_acquire_idempotent_no_orphan(monkeypatch):
    """hunter F1：同实例二次 acquire 幂等返回 True，绝不把本地锁弄成孤儿（release 后必真释放）。"""
    pid = f"_h2re_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(rc, "get_redis", lambda: _OkRedis())
    lock = rc.ModuleLock(pid, "mod")
    assert lock.acquire() is True
    assert lock.acquire() is True, "已持有则幂等返回 True（不二次 acquire threading.Lock）"
    assert lock._held and lock._local_held
    lock.release()
    assert rc._local_lock_for(lock.key).locked() is False, "release 后本地锁真释放（无孤儿死锁）"
    # 释放后同 key 可再获取（证明未孤儿）
    other = rc.ModuleLock(pid, "mod")
    assert other.acquire() is True
    other.release()


def test_h2_local_only_renew_is_noop(monkeypatch):
    """Redis 宕机期获取的纯本地锁 renew 直接 no-op（不对未 SET 的 key 跑 Lua 误判失锁）。"""
    pid = f"_h2r_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(rc, "get_redis", lambda: None)
    lock = rc.ModuleLock(pid, "mod")
    assert lock.acquire() is True and lock._redis_held is False

    # 即便此刻 Redis"恢复"，纯本地锁 renew 仍 no-op True（不走 Redis Lua）
    called = {"n": 0}

    class _CountRedis:
        def eval(self, *a, **k):
            called["n"] += 1
            return 0

    monkeypatch.setattr(rc, "get_redis", lambda: _CountRedis())
    assert lock.renew() is True
    assert called["n"] == 0, "纯本地锁 renew 绝不碰 Redis（否则对未 SET 的 key 恒判失锁）"
    lock.release()


if __name__ == "__main__":
    print("run via pytest")
