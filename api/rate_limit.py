"""api/rate_limit.py — P2-E 进程内令牌桶限流。

单进程拓扑：进程内令牌桶足矣（多副本再换 Redis 令牌桶）。给重型端点（KB 检索/预处理/worker）
提供【每主体（用户 or IP）+ 每端点】的限流依赖，防单一调用方打爆下游 LLM/沙箱/DB。

设计：经典令牌桶——容量 burst、每秒回填 rate。超限返回 429 + Retry-After。fail-open：限流器
自身异常绝不阻断正常请求（可用性优先于限流）。线程/协程安全用简单锁（进程内争用极短）。
"""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request


class _TokenBucket:
    __slots__ = ("capacity", "rate", "_tokens", "_last")

    def __init__(self, capacity: float, rate: float) -> None:
        self.capacity = float(capacity)
        self.rate = float(rate)
        self._tokens = float(capacity)
        self._last = time.monotonic()

    def take(self, now: float) -> tuple[bool, float]:
        """尝试取 1 个令牌。返回 (allowed, retry_after_seconds)。"""
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True, 0.0
        deficit = 1.0 - self._tokens
        retry_after = deficit / self.rate if self.rate > 0 else 60.0
        return False, retry_after


# 复核 F4：桶字典上限——防 IP 轮转(尤其 IPv6 /64)刷爆内存拖垮进程。超限时清扫【已满桶】
# （tokens 已回填到 capacity = 自上次使用起闲置足够久，重建等价、删之无害）。
_MAX_BUCKETS = max(1000, int(__import__("os").environ.get("SWARM_RATELIMIT_MAX_BUCKETS", "50000")))


class RateLimiter:
    """按 key 维护独立令牌桶。key = f"{scope}:{subject}"（端点+主体）。"""

    def __init__(self) -> None:
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = threading.Lock()

    def _evict_idle(self, now: float) -> None:
        """删除已回填至满的闲置桶（不持任何限流状态，重建行为一致）。在锁内调用。"""
        stale = [
            k for k, b in self._buckets.items()
            if min(b.capacity, b._tokens + max(0.0, now - b._last) * b.rate) >= b.capacity
        ]
        for k in stale:
            del self._buckets[k]

    def check(self, key: str, capacity: float, rate: float) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            if len(self._buckets) >= _MAX_BUCKETS and key not in self._buckets:
                self._evict_idle(now)
            b = self._buckets.get(key)
            if b is None:
                b = _TokenBucket(capacity, rate)
                self._buckets[key] = b
            return b.take(now)

    def _reset(self) -> None:
        """测试辅助：清空所有桶。"""
        with self._lock:
            self._buckets.clear()


_limiter = RateLimiter()


def _subject(request: Request) -> str:
    """限流主体：优先已认证用户 id，否则客户端 IP（未登录/公开端点）。"""
    try:
        user = getattr(request.state, "user", None)
        if user is not None and getattr(user, "id", None):
            return f"u:{user.id}"
    except Exception:  # noqa: BLE001
        pass
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def rate_limit(scope: str, capacity: int = 30, rate: float = 1.0):
    """FastAPI 依赖工厂：给端点加【每主体】令牌桶限流。

    capacity=突发上限（桶容量），rate=每秒回填。超限抛 429 + Retry-After。
    fail-open：限流器内部异常一律放行（可用性优先）。环境变量 SWARM_RATELIMIT_DISABLED=1 全局关闭。
    """
    import os

    def _dep(request: Request) -> None:
        if os.environ.get("SWARM_RATELIMIT_DISABLED", "").lower() in ("1", "true", "yes"):
            return
        try:
            key = f"{scope}:{_subject(request)}"
            allowed, retry_after = _limiter.check(key, capacity, rate)
        except Exception:  # noqa: BLE001 — 限流器故障绝不阻断业务
            return
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后再试",
                headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
            )

    return _dep
