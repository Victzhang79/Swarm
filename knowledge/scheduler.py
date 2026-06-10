"""知识库增量更新调度 — 共享 Updater + PG 队列后台消费。"""

from __future__ import annotations

import asyncio
import logging

from swarm.knowledge.updater import KnowledgeUpdater, UpdateEvent, dedupe_event

logger = logging.getLogger(__name__)

_updater: KnowledgeUpdater | None = None
_polling_started = False


async def get_shared_updater() -> KnowledgeUpdater:
    """进程内单例 KnowledgeUpdater（复用 DB/索引连接）。"""
    global _updater
    if _updater is None:
        _updater = KnowledgeUpdater()
        await _updater.connect()
    return _updater


async def start_kb_update_scheduler(*, interval_seconds: int = 5) -> None:
    """API startup 启动 PG 队列轮询。"""
    global _polling_started
    if _polling_started:
        return
    _polling_started = True

    async def _loop() -> None:
        while True:
            try:
                updater = await get_shared_updater()
                processed = await updater.process_pending_events()
                if processed:
                    logger.info("[KBScheduler] processed %d queued events", processed)
            except Exception as exc:
                logger.exception("[KBScheduler] polling error: %s", exc)
            await asyncio.sleep(interval_seconds)

    asyncio.create_task(_loop())
    logger.info("[KBScheduler] started (interval=%ds)", interval_seconds)


async def enqueue_kb_update(event: UpdateEvent) -> int:
    """去重后入队，返回 event id。"""
    updater = await get_shared_updater()
    return await updater.enqueue_event(dedupe_event(event))


async def shutdown_kb_scheduler() -> None:
    """关闭共享 updater（测试/进程退出）。"""
    global _updater, _polling_started
    if _updater is not None:
        await _updater.close()
        _updater = None
    _polling_started = False
