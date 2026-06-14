"""分布式协调后端 — A1 批2 地基（Q1: 全 PG + 可热拔插抽象）。

CoordinationBackend 是协调原语的抽象接口（leader election / lock / lease）。
当前提供 PgCoordinationBackend（PG advisory lock）；将来需要时可加
RedisCoordinationBackend，业务侧（SchedulerLeadership 等）不感知实现。

设计要点：
- leader election 用 PG 会话级 advisory lock：pg_try_advisory_lock 持锁直到
  会话(连接)断开或显式 unlock。因此必须用【专属长生命周期连接】持锁，绝不能用
  连接池的连接（归还后锁归属不确定）。
- 连接断开 → 锁自动释放 → 其它副本可接管，天然无脑裂（advisory lock 是会话绑定）。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


def _key_to_int(key: str) -> int:
    """把字符串 lock key 稳定映射到 64-bit 有符号整数（pg advisory lock 要 bigint）。

    用 blake2b 而非内置 hash()（hash 受 PYTHONHASHSEED 随机化，跨进程不一致——
    那会导致不同副本对同一逻辑锁算出不同 key，选主失效）。
    """
    import hashlib

    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    val = int.from_bytes(digest, "big", signed=False)
    # 映射到 signed 64-bit 范围
    return val - (1 << 63)


class CoordinationBackend(ABC):
    """协调后端抽象。当前实现 PgCoordinationBackend；预留 Redis 等扩展。"""

    @abstractmethod
    async def try_acquire_leadership(self, key: str) -> bool:
        """尝试获取某 key 的 leadership。成功 True（持有至 release/连接断）。"""

    @abstractmethod
    async def release_leadership(self, key: str) -> None:
        """释放 leadership（幂等）。"""

    @abstractmethod
    async def is_held(self, key: str) -> bool:
        """本后端当前是否持有该 key 的 leadership。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭后端（释放所有锁 + 连接）。"""


class PgCoordinationBackend(CoordinationBackend):
    """PG advisory lock 实现。持有一个专属长连接，在其上加会话级 advisory lock。"""

    def __init__(self, postgres_uri: str | None = None) -> None:
        self._uri = postgres_uri
        self._conn = None  # 专属长生命周期连接
        self._held: set[str] = set()

    async def _ensure_conn(self):
        if self._conn is None or self._conn.closed:
            import psycopg

            from swarm.config.settings import DatabaseConfig

            uri = self._uri or DatabaseConfig().postgres_uri
            # autocommit：advisory lock 立即生效，不被事务边界影响
            self._conn = await psycopg.AsyncConnection.connect(uri, autocommit=True)
        return self._conn

    async def try_acquire_leadership(self, key: str) -> bool:
        if key in self._held:
            return True
        try:
            conn = await self._ensure_conn()
            lock_id = _key_to_int(key)
            async with conn.cursor() as cur:
                await cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                row = await cur.fetchone()
            acquired = bool(row and row[0])
            if acquired:
                self._held.add(key)
            return acquired
        except Exception as exc:  # noqa: BLE001
            logger.warning("[coordination] try_acquire_leadership(%s) 失败: %s", key, exc)
            return False

    async def release_leadership(self, key: str) -> None:
        if key not in self._held:
            return
        try:
            conn = await self._ensure_conn()
            lock_id = _key_to_int(key)
            async with conn.cursor() as cur:
                await cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[coordination] release_leadership(%s) 失败: %s", key, exc)
        finally:
            self._held.discard(key)

    async def is_held(self, key: str) -> bool:
        return key in self._held

    async def close(self) -> None:
        # 关连接会自动释放该会话所有 advisory lock（其它副本可接管）
        self._held.clear()
        if self._conn is not None and not self._conn.closed:
            try:
                await self._conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[coordination] close 连接失败: %s", exc)
        self._conn = None
