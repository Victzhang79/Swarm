"""项目级知识维护写屏障。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import AsyncIterator, Any

logger = logging.getLogger(__name__)


class ProjectKnowledgeUpdateRejected(RuntimeError):
    """项目不存在或已进入删除围栏。"""


class ProjectKnowledgeWriterBusy(RuntimeError):
    """同项目已有互斥写者，当前维护任务应稍后重试。"""


class ProjectKnowledgeLeaseLost(ProjectKnowledgeWriterBusy):
    """维护期间项目宽锁已失效，当前写者必须停止。"""


@asynccontextmanager
async def project_knowledge_write_fence(
    project_id: str,
    *,
    operation: str,
    ttl_sec: float = 3600,
    renew_interval_seconds: float | None = None,
    project_loader: Callable[[str], dict[str, Any] | None] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """持有项目宽锁，并在锁内复读项目持久状态后才允许知识层写入。"""
    import asyncio

    from swarm.infra.cancellation import (
        cancel_and_wait,
        run_blocking_owned,
        run_db_blocking_owned,
    )
    from swarm.infra.redis_client import ModuleLock, renew_interval_sec
    from swarm.project import store

    lock = ModuleLock(project_id, "default", ttl_sec=ttl_sec)
    acquired = await run_blocking_owned(
        lock.acquire,
        operation=f"{operation} 获取项目知识写锁 project={project_id}",
        cancel_result_cleanup=lambda result: lock.release() if result else None,
    )
    if not acquired:
        raise ProjectKnowledgeWriterBusy(
            f"项目 {project_id} 正由其他写者维护"
        )
    owner = asyncio.current_task()
    renew_stop = asyncio.Event()
    lease_lost = False
    fence_cancel_issued = False
    cancel_seen = False

    async def _renew_loop() -> None:
        nonlocal fence_cancel_issued, lease_lost
        interval = (
            renew_interval_seconds
            if renew_interval_seconds is not None
            else renew_interval_sec(lock.ttl_sec)
        )
        interval = max(0.01, float(interval))
        while True:
            try:
                await asyncio.wait_for(renew_stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await run_blocking_owned(
                    lock.renew,
                    operation=f"{operation} 续期项目知识写锁 project={project_id}",
                )
            except Exception:  # noqa: BLE001 — 续期机制异常也必须按失锁 fail-closed
                logger.exception(
                    "%s 续期项目知识写锁异常，按失锁停止 writer project=%s",
                    operation,
                    project_id,
                )
                renewed = False
            if not renewed:
                lease_lost = True
                if owner is not None and not owner.done():
                    fence_cancel_issued = bool(owner.cancel())
                return

    renew_task: asyncio.Task[None] | None = None
    try:
        project = await run_db_blocking_owned(
            project_loader or store.get_project,
            project_id,
            operation=f"{operation} 锁内复读项目 project={project_id}",
        )
        if not project or project.get("status") == "DELETING":
            raise ProjectKnowledgeUpdateRejected(
                f"项目 {project_id} 不可接受知识库更新"
            )
        renew_task = asyncio.create_task(_renew_loop())
        try:
            yield project
        except asyncio.CancelledError:
            cancel_seen = True
    finally:
        renew_stop.set()
        try:
            try:
                if renew_task is not None:
                    await cancel_and_wait(
                        renew_task,
                        operation=f"{operation} 项目知识写锁续期任务 project={project_id}",
                    )
            except asyncio.CancelledError:
                cancel_seen = True
        finally:
            try:
                await run_blocking_owned(
                    lock.release,
                    operation=f"{operation} 释放项目知识写锁 project={project_id}",
                )
            except asyncio.CancelledError:
                # run_blocking_owned 已排空 release；这里只延迟控制流，统一在下方
                # 区分 fence 自发取消与外部取消。
                cancel_seen = True

        remaining_cancels = owner.cancelling() if owner is not None else 0
        if fence_cancel_issued and owner is not None:
            # Task.cancel 没有来源标签；本 fence 只撤销自己恰好发出的一次请求。
            # 若还有计数，说明外部同时取消，必须保留原 CancelledError 语义。
            remaining_cancels = owner.uncancel()
        if cancel_seen and (not fence_cancel_issued or remaining_cancels > 0):
            raise asyncio.CancelledError
        if lease_lost:
            raise ProjectKnowledgeLeaseLost(
                f"项目 {project_id} 知识写锁已失效"
            ) from None
        if cancel_seen:
            raise asyncio.CancelledError
