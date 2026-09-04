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

import asyncio
import logging
import math
import os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_OPERATION_TIMEOUT_S = 2.0


class CoordinationOperationTimeout(TimeoutError):
    """协调操作超过独立预算；调用方必须按不可用/失主处理。"""


def coordination_operation_timeout_s() -> float:
    """协调后端单次操作预算；坏值不能恢复成无限等待。"""
    raw = os.environ.get("SWARM_COORDINATION_OPERATION_TIMEOUT_SEC")
    if raw is None or not raw.strip():
        return _DEFAULT_OPERATION_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    if math.isfinite(value) and value > 0:
        return value
    logger.warning(
        "SWARM_COORDINATION_OPERATION_TIMEOUT_SEC=%r 非法，回退默认 %.0fs",
        raw,
        _DEFAULT_OPERATION_TIMEOUT_S,
    )
    return _DEFAULT_OPERATION_TIMEOUT_S


def _consume_task_result(task: asyncio.Task) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


async def _invalidate_timed_out_backend(
    backend: Any,
    timeout_s: float,
    operation_epoch: Any,
) -> None:
    """同步撤销本地 ownership，再有界关闭半开连接。"""
    invalidate = getattr(backend, "invalidate_after_timeout", None)
    cleanup = (
        invalidate(operation_epoch) if callable(invalidate) else backend.close()
    )
    cleanup_task = asyncio.create_task(cleanup)
    done, _pending = await asyncio.wait(
        {cleanup_task}, timeout=min(timeout_s, 0.25),
    )
    if cleanup_task not in done:
        cleanup_task.cancel()
        cleanup_task.add_done_callback(_consume_task_result)
    else:
        # 已在 250ms 内结束也必须取 result；close/invalidate 的异常否则只会在 GC 时以
        # “never retrieved” 形式迟到，丢失当前 coordination timeout 的因果上下文。
        try:
            cleanup_task.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[coordination] 超时连接清理失败: %s", exc)


async def run_coordination_operation(
    backend: Any,
    operation: str,
    *args: Any,
) -> Any:
    """有界执行协调操作；超时即 fail-closed，并撤销本地锁/连接所有权。"""
    timeout_s = coordination_operation_timeout_s()
    epoch_reader = getattr(backend, "coordination_operation_epoch", None)
    operation_epoch = epoch_reader() if callable(epoch_reader) else None
    task = asyncio.create_task(getattr(backend, operation)(*args))
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout_s)
    except asyncio.CancelledError:
        # 调用方取消不是 leadership 失效证据。只取消本次探测，不能关闭其它协程共享的
        # 健康 backend/连接，否则一个 HTTP disconnect 就能主动打掉本副本 leader。
        task.cancel()
        task.add_done_callback(_consume_task_result)
        raise
    if task in done:
        return task.result()

    task.cancel()
    task.add_done_callback(_consume_task_result)
    logger.error(
        "[coordination] %s 超时 %.3fs，判定失主并废弃协调连接",
        operation,
        timeout_s,
    )
    await _invalidate_timed_out_backend(backend, timeout_s, operation_epoch)
    raise CoordinationOperationTimeout(
        f"coordination {operation} timed out after {timeout_s:.3f}s"
    )


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

    async def verify_leadership(self, key: str) -> bool:
        """D38：主动校验 leadership 仍然有效（含底层会话/连接真实存活探测）。

        默认实现退化为 is_held（内存型后端够用）；有真实会话语义的后端（PG advisory
        lock）必须覆写为带探活查询的版本——连接对象自称 open 但服务端已断（半开连接）
        时须判失主。供 leader 心跳看门狗调用。"""
        return await self.is_held(key)

    async def probe(self) -> None:
        """启动健康探测；实现必须让连接/查询错误向上冒泡。"""
        await self.is_held("scheduler:_probe_")

    @abstractmethod
    async def close(self) -> None:
        """关闭后端（释放所有锁 + 连接）。"""

    def coordination_operation_epoch(self):
        """用于防止旧操作超时清掉后续新建的协调连接。"""
        return None

    def invalidate_after_timeout(self, _operation_epoch=None):
        """超时后的失主处理；自定义后端至少关闭自身。"""
        return self.close()


class PgCoordinationBackend(CoordinationBackend):
    """PG advisory lock 实现。持有一个专属长连接，在其上加会话级 advisory lock。"""

    def __init__(self, postgres_uri: str | None = None) -> None:
        self._uri = postgres_uri
        self._conn = None  # 专属长生命周期连接
        self._held: set[str] = set()
        self._connection_generation = 0
        # 首次连接/断线重连必须 singleflight：否则两个并发 acquire 会各自在不同
        # PG session 上拿锁，而单一 _conn/_held 只能记住其中一条，形成幽灵 ownership。
        self._connection_lock = asyncio.Lock()

    def _is_current_connection_epoch(self, conn: Any, generation: int) -> bool:
        return self._connection_generation == generation and self._conn is conn

    async def _discard_failed_connection(self, conn: Any, generation: int) -> None:
        """查询错误只撤销其所属 epoch；旧错误不得污染后来重连的 ownership。"""
        if self._is_current_connection_epoch(conn, generation):
            self._held.clear()
            self._conn = None
            self._connection_generation += 1
        try:
            if conn is not None and not conn.closed:
                await conn.close()
        except Exception:  # noqa: BLE001
            pass

    async def _ensure_conn(self):
        if self._conn is not None and not self._conn.closed:
            return self._conn
        async with self._connection_lock:
            # 锁内二次检查：等待 singleflight 的其它调用直接复用已发布连接。
            if self._conn is not None and not self._conn.closed:
                return self._conn
            # P1-DEBT-13：连接断开/重建意味着旧【会话级】advisory lock 已被 PG 自动释放。
            # 此时本地 _held 全部失效，必须清空——否则重连后 is_held/try_acquire 会沿用旧
            # 标记误报仍是 leader，与已接管的另一副本同时自认 leader（脑裂）。重连后须在
            # 【新会话】上重新 pg_try_advisory_lock 才算真正持锁。
            replacing_connection = self._conn is not None
            if replacing_connection:
                self._conn = None
                self._connection_generation += 1
            if self._held:
                logger.warning(
                    "[coordination] 协调连接重建，清空 %d 个失效的本地持锁标记并需重新选主（防脑裂）",
                    len(self._held),
                )
                self._held.clear()
            import psycopg

            from swarm.config.settings import DatabaseConfig

            from swarm.infra.db import pg_connect_timeout_kwargs

            uri = self._uri or DatabaseConfig().postgres_uri
            # autocommit：advisory lock 立即生效，不被事务边界影响
            # D15：直连补 connect_timeout——PG 网络黑洞时有界快失败，不无限挂。
            publish_generation = self._connection_generation
            conn = await psycopg.AsyncConnection.connect(
                uri, autocommit=True, **pg_connect_timeout_kwargs()
            )
            # invalidate_after_timeout/close 可在 connect await 期间同步推进 epoch。
            # 旧连接建立结果此时已无发布权，必须关掉而不是覆盖新 epoch。
            if publish_generation != self._connection_generation:
                try:
                    await conn.close()
                finally:
                    raise ConnectionError(
                        "coordination connection epoch changed during connect"
                    )
            self._conn = conn
            return conn

    async def try_acquire_leadership(self, key: str) -> bool:
        # D38：早退必须以【连接存活】为前提——连接断开意味着会话级 advisory lock 已被
        # PG 服务端释放，本地 _held 是失效标记；此前 is_held 修了这个早退、这里没修，
        # PG 重启后旧 leader 凭旧标记恒 True 谎报仍持锁 → 与新 leader 双跑（脑裂）。
        # 连接断时落到 _ensure_conn：它会清空 _held、重连并在【新会话】上真正重新抢锁。
        if key in self._held and self._conn is not None and not self._conn.closed:
            return True
        conn = None
        connection_generation = -1
        try:
            conn = await self._ensure_conn()
            connection_generation = self._connection_generation
            lock_id = _key_to_int(key)
            async with conn.cursor() as cur:
                await cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                row = await cur.fetchone()
            acquired = bool(row and row[0])
            if not self._is_current_connection_epoch(conn, connection_generation):
                return False
            if acquired:
                self._held.add(key)
            return acquired
        except Exception as exc:  # noqa: BLE001
            logger.warning("[coordination] try_acquire_leadership(%s) 失败: %s", key, exc)
            if conn is not None:
                await self._discard_failed_connection(conn, connection_generation)
            return False

    async def probe(self) -> None:
        """不吞异常的 PG 健康探测，供 startup 区分锁竞争与协调不可用。"""
        conn = await self._ensure_conn()
        generation = self._connection_generation
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                row = await cur.fetchone()
            if not row or row[0] != 1:
                raise ConnectionError("coordination probe returned invalid result")
            if not self._is_current_connection_epoch(conn, generation):
                raise ConnectionError("coordination connection epoch changed during probe")
        except Exception:
            await self._discard_failed_connection(conn, generation)
            raise

    async def release_leadership(self, key: str) -> None:
        if key not in self._held:
            return
        conn = None
        connection_generation = -1
        try:
            conn = await self._ensure_conn()
            connection_generation = self._connection_generation
            lock_id = _key_to_int(key)
            async with conn.cursor() as cur:
                await cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[coordination] release_leadership(%s) 失败: %s", key, exc)
            if conn is not None:
                await self._discard_failed_connection(conn, connection_generation)
        finally:
            if (
                conn is None
                or self._is_current_connection_epoch(conn, connection_generation)
            ):
                self._held.discard(key)

    async def is_held(self, key: str) -> bool:
        # P1-DEBT-13：连接断开 → 该会话所有 advisory lock 已被 PG 释放，本地标记失效。
        # 必须校验【真实连接存活】，不能只查本地 _held（否则连接断后仍误报持锁→脑裂）。
        if self._conn is None or self._conn.closed:
            if self._held:
                self._held.clear()
            return False
        return key in self._held

    async def verify_leadership(self, key: str) -> bool:
        """D38：leader 心跳探活。会话级 advisory lock 与 PG 会话同生死——
        只要【真实会话存活】且本地持锁标记在，即仍是 leader；探活查询失败
        （半开连接：对象自称 open 但服务端已断/重启）→ 锁已被服务端释放，判失主，
        清空本地标记并弃掉死连接（下次 try_acquire 经 _ensure_conn 重连重新竞选）。"""
        if key not in self._held:
            return False
        if self._conn is None or self._conn.closed:
            self._held.clear()
            return False
        conn = self._conn
        connection_generation = self._connection_generation
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[coordination] verify_leadership(%s) 探活失败，判定失主: %s", key, exc)
            # 探活期间可能已有其它协程完成重连。旧查询失败只能废弃它自己的连接；
            # generation + identity 均仍匹配时，才有权撤销当前 epoch 的 ownership。
            await self._discard_failed_connection(conn, connection_generation)
            return False
        if not self._is_current_connection_epoch(conn, connection_generation):
            return False
        return key in self._held

    async def close(self) -> None:
        # 关连接会自动释放该会话所有 advisory lock（其它副本可接管）
        self._held.clear()
        conn = self._conn
        self._conn = None
        self._connection_generation += 1
        if conn is not None and not conn.closed:
            try:
                await conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[coordination] close 连接失败: %s", exc)

    def coordination_operation_epoch(self):
        return self._connection_generation, self._conn

    def invalidate_after_timeout(self, operation_epoch=None):
        """在任何 await 前清本地持锁态/摘除连接，防半开会话继续被误认 leader。"""
        expected_generation, expected_conn = (
            operation_epoch
            if isinstance(operation_epoch, tuple) and len(operation_epoch) == 2
            else (self._connection_generation, self._conn)
        )
        is_stale_epoch = (
            expected_generation != self._connection_generation
            or (
                expected_conn is not None
                and self._conn is not expected_conn
            )
        )
        if is_stale_epoch:
            # 旧操作的 deadline 晚到：只能关它开始时看到的旧连接，不得清后来已重连并
            # 重新取得的 held/conn。generation + identity 双守卫避免 ABA 误伤。
            async def _close_stale() -> None:
                if expected_conn is not None and not expected_conn.closed:
                    await expected_conn.close()

            return _close_stale()
        self._held.clear()
        conn = self._conn
        self._conn = None
        self._connection_generation += 1

        async def _close_detached() -> None:
            if conn is not None and not conn.closed:
                await conn.close()

        return _close_detached()
