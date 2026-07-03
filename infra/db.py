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
    SWARM_DB_POOL_MAX (默认 min(32, cpu+4)，对齐 asyncio 默认线程池上限，见 _default_pool_max)
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


def _default_pool_max() -> int:
    """P1-C：默认连接池上限对齐 asyncio 默认线程池上限 min(32, cpu+4)。

    ~142 处 run_in_executor(None, <同步 DB 调用>) + asyncio.to_thread 共享默认线程池
    (~min(32,cpu+4) 线程)。若【连接数 < 可并发发起 DB 调用的线程数】→ 多余线程在
    pool.connection() 排队撞 30s timeout 抛 PoolTimeout（高并发批量失败）。
    治本【安全方向】：把连接数抬到 ≥ 线程数（而非缩线程池——那会饿死沙箱 HTTP/子进程等
    非 DB 阻塞工作，把有界的 30s PoolTimeout 换成无界的全局卡死）。连接按需开（min_size=1，
    空闲仅 1 条），单进程 + PG(max_connections 默认 100) 富余。仍可经 SWARM_DB_POOL_MAX 覆盖。
    """
    return min(32, (os.cpu_count() or 1) + 4)


def _pool_size() -> tuple[int, int]:
    """从环境变量读取连接池上下限（pmax 默认对齐默认线程池上限，见 _default_pool_max）。"""
    try:
        pmin = int(os.environ.get("SWARM_DB_POOL_MIN", "1"))
    except ValueError:
        pmin = 1
    _raw_max = os.environ.get("SWARM_DB_POOL_MAX")
    if _raw_max:
        try:
            pmax = int(_raw_max)
        except ValueError:
            pmax = _default_pool_max()
    else:
        pmax = _default_pool_max()
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


def _connect_timeout() -> float:
    """F2：libpq connect_timeout（秒）——绑住【建立连接】阶段的挂起（PG 不可达/TCP 卡死）。

    注：这是【连接建立】超时，非查询执行超时。全局 statement_timeout 会误杀合法长查询
    （大 KB 操作/迁移），风险高故不默认设；需要时经连接串 options=-c statement_timeout=… 单点加。
    默认 10s；SWARM_DB_CONNECT_TIMEOUT 可调，<=0 关闭（回退 libpq 默认=无限等）。
    """
    try:
        return float(os.environ.get("SWARM_DB_CONNECT_TIMEOUT", "10"))
    except (TypeError, ValueError):
        return 10.0


_POOL_TIMEOUT_SEC = _pool_timeout()
_POOL_MAX_LIFETIME_SEC = _pool_max_lifetime()
_CONNECT_TIMEOUT_SEC = _connect_timeout()


def _conn_kwargs() -> dict:
    """连接级 kwargs：autocommit + connect_timeout（>0 时）。"""
    kw: dict = {"autocommit": True}
    if _CONNECT_TIMEOUT_SEC > 0:
        # libpq connect_timeout 要求【十进制整数】秒（"10.0" 部分 libpq 版本会拒）→ 转 int。
        kw["connect_timeout"] = max(1, int(_CONNECT_TIMEOUT_SEC))
    return kw


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
                kwargs=_conn_kwargs(),
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
            kwargs=_conn_kwargs(),
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
