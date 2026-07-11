"""主题I H-1（外部深审）：Redis 连上后故障导致 acquire 异常外泄 → runner _task_running 残留死锁。

取证：get_redis 一旦连上就缓存 client（redis_client.py:86 `if _redis_client is not None: return`）
从不重探；acquire 的 r.set（:173）在旧实现无 try —— 缓存 client 坏了 SET 抛异常，而 runner
的 module_lock.acquire()（runner.py:1347）在 try（:1362）之外、_task_running.add 已执行（:1338）
→ 异常泄漏使 discard 不执行 → task_id 永判"已在执行中"死锁。
修：acquire/renew/release 的 Redis IO 异常 → _invalidate_redis 作废坏 client + acquire 转本地
锁 fallback，契约=acquire 永不抛只返回 bool。
"""
from __future__ import annotations

import uuid

import swarm.infra.redis_client as rc


class _BrokenClient:
    """模拟 Redis 连上后连接断：所有 IO 抛异常（缓存 client 已坏）。"""
    def set(self, *a, **k):
        raise ConnectionError("Redis connection reset (cached client went bad)")

    def eval(self, *a, **k):
        raise ConnectionError("Redis connection reset")


def test_h1_acquire_never_raises_on_broken_cached_client(monkeypatch):
    """核心：缓存 client 坏时 acquire 绝不抛异常（否则 runner _task_running 残留死锁）。"""
    monkeypatch.setattr(rc, "get_redis", lambda: _BrokenClient())
    pid = f"_h1_{uuid.uuid4().hex[:8]}"
    lock = rc.ModuleLock(pid, "mod")
    # 旧实现：r.set 抛 ConnectionError 向上泄漏。新实现：转本地锁 fallback，返回 bool。
    got = lock.acquire()
    assert isinstance(got, bool), "acquire 契约=永不抛，只返回 bool"
    assert got is True, "坏 client 时应转本地锁 fallback 并（无争用）拿到"
    lock.release()  # release 也不应抛（本地锁路径）


def test_h1_broken_client_invalidated_for_reprobe(monkeypatch):
    """SET 异常必须作废缓存 client（_redis_client=None + 冷却），下次 get_redis 才会重探。"""
    monkeypatch.setattr(rc, "_redis_client", _BrokenClient(), raising=False)
    monkeypatch.setattr(rc, "_redis_unavailable_at", None, raising=False)
    monkeypatch.setattr(rc, "redis_enabled", lambda: True)
    pid = f"_h1_{uuid.uuid4().hex[:8]}"
    lock = rc.ModuleLock(pid, "mod")
    lock.acquire()
    assert rc._redis_client is None, "坏 client 必须被作废（下次 get_redis 重探）"
    assert rc._redis_unavailable_at is not None, "作废须进冷却窗，避免立即又拿到同一坏 client"
    lock.release()


def test_h1_renew_invalidates_on_error_without_breaking_tolerance(monkeypatch):
    """renew 遇 IO 异常作废坏 client，但瞬时容忍语义不变（阈值内仍返回 True 不误杀长任务）。"""
    monkeypatch.setattr(rc, "_redis_client", None, raising=False)
    monkeypatch.setattr(rc, "_redis_unavailable_at", None, raising=False)
    monkeypatch.setattr(rc, "redis_enabled", lambda: True)
    pid = f"_h1_{uuid.uuid4().hex[:8]}"
    lock = rc.ModuleLock(pid, "mod")
    lock._held = True
    lock._local_held = False
    lock._redis_held = True  # H-2 后：只有 _redis_held 的锁 renew 才走 Redis Lua（否则 no-op）
    lock._last_ok_monotonic = 0.0  # 无墙钟基准 → _elapsed=0，不触发墙钟闸
    monkeypatch.setattr(rc, "get_redis", lambda: _BrokenClient())
    ok = lock.renew()
    assert ok is True, "首次瞬时失败在阈值内应容忍（返回 True），不误杀长任务"
    assert rc._redis_client is None, "renew 遇错也应作废坏 client 供重探"


if __name__ == "__main__":
    print("run via pytest")
