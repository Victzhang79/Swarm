"""统一数据库连接池 — 同步 + 异步。

替代各 store 每次操作新建 psycopg.connect() 的反模式。进程内单例连接池，
按连接串缓存（支持多库/测试隔离）。高并发下复用连接、限制总连接数，
避免连接耗尽与建连延迟。

用法（同步）:
    from swarm.infra.db import sync_pool
    with sync_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(...)

用法（异步）:
    from swarm.infra.db import async_pool
    pool = await async_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(...)

连接池参数可通过环境变量调整:
    SWARM_DB_POOL_MIN (默认 1)
    SWARM_DB_POOL_MAX (默认 10)
"""

from __future__ import annotations

import atexit
import asyncio
import logging
import os
import threading

from psycopg_pool import AsyncConnectionPool, ConnectionPool

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)


def _pool_size() -> tuple[int, int]:
    """从环境变量读取连接池上下限。"""
    try:
        pmin = int(os.environ.get("SWARM_DB_POOL_MIN", "1"))
    except ValueError:
        pmin = 1
    try:
        pmax = int(os.environ.get("SWARM_DB_POOL_MAX", "10"))
    except ValueError:
        pmax = 10
    # M4 修复：保证 pmin ≤ pmax，否则 ConnectionPool(min>max) 启动即崩。
    pmin = max(0, pmin)
    pmax = max(1, pmax)
    if pmin > pmax:
        pmin = pmax
    return pmin, pmax


# A-P1-16：连接池安全参数。可经环境变量覆盖。
def _pool_timeout() -> float:
    try:
        return float(os.environ.get("SWARM_DB_POOL_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30.0


def _pool_max_lifetime() -> float:
    try:
        return float(os.environ.get("SWARM_DB_POOL_MAX_LIFETIME", "3600"))
    except (TypeError, ValueError):
        return 3600.0


_POOL_TIMEOUT_SEC = _pool_timeout()
_POOL_MAX_LIFETIME_SEC = _pool_max_lifetime()


def _default_conn_str() -> str:
    return DatabaseConfig().postgres_uri


# ── 同步连接池（按连接串缓存）──────────────────────

_sync_pools: dict[str, "ConnectionPool"] = {}
_sync_lock = threading.Lock()


def sync_pool(conn_str: str | None = None) -> ConnectionPool:
    """获取/创建同步连接池（autocommit）。

    返回的池对象用 `with pool.connection() as conn:` 取连接，
    退出时连接归还池而非关闭。
    """
    conn_str = conn_str or _default_conn_str()
    pool = _sync_pools.get(conn_str)
    if pool is not None:
        return pool
    with _sync_lock:
        pool = _sync_pools.get(conn_str)
        if pool is None:
            pmin, pmax = _pool_size()
            pool = ConnectionPool(
                conninfo=conn_str,
                min_size=pmin,
                max_size=pmax,
                kwargs={"autocommit": True},
                # A-P1-16：原先未设安全参数 → 池耗尽时 connection() 无限阻塞、
                # 半开/陈旧连接永不校验回收。
                # timeout：取连接最多等 30s，超时抛 PoolTimeout 而非永久挂起。
                # max_lifetime：连接最多存活 1h 后回收，避免长寿连接累积问题。
                # check：归还/取出时校验连接活性，dead conn 自动回收重建。
                timeout=_POOL_TIMEOUT_SEC,
                max_lifetime=_POOL_MAX_LIFETIME_SEC,
                check=ConnectionPool.check_connection,
                open=True,
                name="swarm-sync",
            )
            _sync_pools[conn_str] = pool
            logger.info("[db] sync pool created (min=%d max=%d)", pmin, pmax)
    return pool


# ── 异步连接池（按连接串缓存）──────────────────────

_async_pools: dict[str, AsyncConnectionPool] = {}
_async_lock: "asyncio.Lock | None" = None


def _get_async_lock() -> "asyncio.Lock":
    # 惰性创建（避免 import 时无事件循环）。同一事件循环内单例即可。
    global _async_lock
    if _async_lock is None:
        _async_lock = asyncio.Lock()
    return _async_lock


async def async_pool(conn_str: str | None = None) -> AsyncConnectionPool:
    """获取/创建异步连接池（autocommit）。"""
    conn_str = conn_str or _default_conn_str()
    pool = _async_pools.get(conn_str)
    if pool is not None:
        return pool
    # H4 修复：原无锁 check-then-act，两协程同时见 None 各建一池并 await open()，
    # 一个被字典覆盖后永不关闭 → 连接池泄漏。加锁 + 锁内复查（照 sync_pool 做法）。
    async with _get_async_lock():
        pool = _async_pools.get(conn_str)
        if pool is not None:
            return pool
        pmin, pmax = _pool_size()
        pool = AsyncConnectionPool(
            conninfo=conn_str,
            min_size=pmin,
            max_size=pmax,
            kwargs={"autocommit": True},
            # A-P1-16：同 sync pool，设超时/最大寿命/活性校验。
            timeout=_POOL_TIMEOUT_SEC,
            max_lifetime=_POOL_MAX_LIFETIME_SEC,
            check=AsyncConnectionPool.check_connection,
            open=False,
            name="swarm-async",
        )
        await pool.open()
        _async_pools[conn_str] = pool
        logger.info("[db] async pool created (min=%d max=%d)", pmin, pmax)
        return pool


# ── 关闭（测试/进程退出）──────────────────────────

def close_sync_pools() -> None:
    with _sync_lock:
        for pool in _sync_pools.values():
            try:
                pool.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[db] close sync pool: %s", exc)
        _sync_pools.clear()


async def close_async_pools() -> None:
    for pool in list(_async_pools.values()):
        try:
            await pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[db] close async pool: %s", exc)
    _async_pools.clear()


# 进程退出时优雅关闭同步池（避免后台 worker 线程在解释器关闭时报错）
atexit.register(close_sync_pools)
