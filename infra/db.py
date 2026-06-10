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
    return max(0, pmin), max(1, pmax)


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
                open=True,
                name="swarm-sync",
            )
            _sync_pools[conn_str] = pool
            logger.info("[db] sync pool created (min=%d max=%d)", pmin, pmax)
    return pool


# ── 异步连接池（按连接串缓存）──────────────────────

_async_pools: dict[str, AsyncConnectionPool] = {}


async def async_pool(conn_str: str | None = None) -> AsyncConnectionPool:
    """获取/创建异步连接池（autocommit）。"""
    conn_str = conn_str or _default_conn_str()
    pool = _async_pools.get(conn_str)
    if pool is not None:
        return pool
    pmin, pmax = _pool_size()
    pool = AsyncConnectionPool(
        conninfo=conn_str,
        min_size=pmin,
        max_size=pmax,
        kwargs={"autocommit": True},
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
