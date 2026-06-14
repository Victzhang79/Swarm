"""调度器选主封装 — A1 批2 地基（Q3: 仅 leader 执行 + 服务化地基）。

SchedulerLeadership 把"周期性任务仅由 leader 副本执行"的模式封装成一处，
4 个后台调度器（task/kb_update/consistency/memory_decay）复用它。

服务化地基（Q3）：将来把调度器拆成独立进程时，那个进程永远是 leader——
只需给它一个 always-leader 的 backend（或直接 is_leader() 返回 True），
调度器循环逻辑零改动。

降级（设计文档批2步骤4）：backend 为 None 或不可用时，退化为"本进程即 leader"
（单机行为不变，开箱即用）。
"""

from __future__ import annotations

import asyncio
import logging

from swarm.infra.coordination import CoordinationBackend

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
        self._is_leader = await self._backend.try_acquire_leadership(self._key)
        return self._is_leader

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def release(self) -> None:
        if self._backend is not None:
            await self._backend.release_leadership(self._key)
        self._is_leader = False


# ── 进程级共享的协调后端单例（与 app 生命周期对齐）──────────────

_backend: CoordinationBackend | None = None


async def init_coordination_backend(postgres_uri: str | None = None) -> CoordinationBackend | None:
    """A1 批2：startup 内初始化进程级协调后端。失败返回 None（降级单进程即 leader）。"""
    global _backend
    if _backend is not None:
        return _backend
    try:
        from swarm.infra.coordination import PgCoordinationBackend

        be = PgCoordinationBackend(postgres_uri)
        # 探活：尝试一次无害的 leadership 探测连接可用性（用临时 key 立即释放）
        probe_key = "scheduler:_probe_"
        ok = await be.try_acquire_leadership(probe_key)
        if ok:
            await be.release_leadership(probe_key)
        _backend = be
        logger.info("[A1] 协调后端(PG advisory lock)已初始化")
        return _backend
    except Exception as exc:  # noqa: BLE001
        logger.warning("[A1] 协调后端初始化失败，调度器降级单进程即 leader: %s", exc)
        _backend = None
        return None


def get_coordination_backend() -> CoordinationBackend | None:
    return _backend


async def close_coordination_backend() -> None:
    global _backend
    if _backend is not None:
        try:
            await _backend.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[A1] 关闭协调后端失败: %s", exc)
        _backend = None


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
