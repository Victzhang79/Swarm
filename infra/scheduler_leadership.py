"""调度器选主封装 — A1 批2 地基（Q3: 仅 leader 执行 + 服务化地基）。

SchedulerLeadership 把"周期性任务仅由 leader 副本执行"的模式封装成一处，
4 个后台调度器（task/kb_update/consistency/memory_decay）复用它。

服务化地基（Q3）：将来把调度器拆成独立进程时，那个进程永远是 leader——
只需给它一个 always-leader 的 backend（或直接 is_leader() 返回 True），
调度器循环逻辑零改动。

显式构造 backend=None 时保留单进程模式；但应用启动时 PG 协调后端探活失败必须
fail-closed，绝不能把“初始化失败”与“明确单机”都编码成 None，否则多副本会同时自认 leader。
"""

from __future__ import annotations

import asyncio
import logging

from swarm.infra.coordination import (
    CoordinationBackend,
    CoordinationOperationTimeout,
    run_coordination_operation,
)

logger = logging.getLogger(__name__)


class SchedulerLeadership:
    """某个调度器的选主句柄。

    用法：
        lead = SchedulerLeadership(backend, "scheduler:consistency")
        if await lead.acquire_or_wait():
            # 本副本是 leader，执行任务
        # 退出/结束时 await lead.release()
    """

    def __init__(self, backend: CoordinationBackend | None, key: str) -> None:
        self._backend = backend
        self._key = key
        self._is_leader = False

    async def try_become_leader(self) -> bool:
        """尝试成为 leader。backend 缺失 → 降级为本进程即 leader（单机不变）。"""
        if self._backend is None:
            self._is_leader = True
            return True
        try:
            self._is_leader = bool(await run_coordination_operation(
                self._backend, "try_acquire_leadership", self._key))
        except CoordinationOperationTimeout:
            self._is_leader = False
        return self._is_leader

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def still_leader(self) -> bool:
        """D38：心跳校验 leadership 仍有效（backend 缺失=单机降级恒 leader）。
        失主时同步翻转 is_leader，供看门狗停调度器。"""
        if self._backend is None:
            return self._is_leader
        try:
            ok = await run_coordination_operation(
                self._backend, "verify_leadership", self._key)
        except CoordinationOperationTimeout:
            ok = False
        self._is_leader = bool(ok)
        return self._is_leader

    async def release(self) -> None:
        if self._backend is not None:
            try:
                await run_coordination_operation(
                    self._backend, "release_leadership", self._key)
            except CoordinationOperationTimeout:
                pass
        self._is_leader = False


# ── 进程级共享的协调后端单例（与 app 生命周期对齐）──────────────

_backend: CoordinationBackend | None = None


async def init_coordination_backend(postgres_uri: str | None = None) -> CoordinationBackend | None:
    """startup 初始化进程级协调后端；探活失败向上抛，让应用拒绝带病启动。"""
    global _backend
    if _backend is not None:
        return _backend
    be: CoordinationBackend | None = None
    try:
        from swarm.infra.coordination import PgCoordinationBackend

        be = PgCoordinationBackend(postgres_uri)
        # 独立 probe 必须让连接/SQL 异常向上冒泡；try_acquire 的 False 同时表示“锁竞争”
        # 与“内部已吞异常”，不能承担 startup 健康判据。
        await run_coordination_operation(be, "probe")
        _backend = be
        logger.info("[A1] 协调后端(PG advisory lock)已初始化")
        return _backend
    except Exception as exc:  # noqa: BLE001
        # backend=None 仍是显式单机构造的合法语义，但真实 startup 初始化失败不能回落到
        # 同一个值：否则 N 个副本都会走 SchedulerLeadership(None) 并各自成为 leader。
        if be is not None:
            try:
                await asyncio.wait_for(be.close(), timeout=0.25)
            except Exception:  # noqa: BLE001 — 原始探活异常优先，关闭仅 best-effort
                logger.warning("[A1] 协调后端探活失败后的连接清理未完成", exc_info=True)
        _backend = None
        logger.error("[A1] 协调后端初始化失败，fail-closed 拒绝启动: %s", exc)
        raise


def get_coordination_backend() -> CoordinationBackend | None:
    return _backend


async def close_coordination_backend() -> None:
    global _backend
    if _backend is not None:
        backend = _backend
        _backend = None
        close_task = asyncio.create_task(backend.close())
        try:
            from swarm.infra.coordination import coordination_operation_timeout_s

            done, _pending = await asyncio.wait(
                {close_task}, timeout=coordination_operation_timeout_s()
            )
            if close_task not in done:
                close_task.cancel()

                def _consume(task: asyncio.Task) -> None:
                    try:
                        task.exception()
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

                close_task.add_done_callback(_consume)
                logger.warning("[A1] 关闭协调后端超时，已摘除本地引用并继续停机")
                return
            close_task.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[A1] 关闭协调后端失败: %s", exc)


def make_leadership(key: str) -> SchedulerLeadership:
    """便捷工厂：用进程级后端创建某调度器的选主句柄。"""
    return SchedulerLeadership(_backend, key)


async def run_as_leader_loop(
    key: str,
    interval_seconds: float,
    task_fn,
    *,
    recheck_seconds: float | None = None,
) -> None:
    """通用'仅 leader 执行'循环：非 leader 时定期重试抢主，leader 时按 interval 执行 task_fn。

    Args:
        key: leadership key
        interval_seconds: leader 执行 task_fn 的间隔
        task_fn: async 无参回调（执行一次调度工作）
        recheck_seconds: 非 leader 时重试抢主的间隔（默认 = interval 与 30s 的较小值）
    """
    lead = make_leadership(key)
    recheck = recheck_seconds or min(interval_seconds, 30.0)
    while True:
        became = await lead.try_become_leader()
        if not became:
            await asyncio.sleep(recheck)
            continue
        try:
            await task_fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[scheduler:%s] task 执行异常: %s", key, exc)
        await asyncio.sleep(interval_seconds)
