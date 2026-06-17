"""知识库增量更新调度 — 共享 Updater + PG 队列后台消费。"""

from __future__ import annotations

import asyncio
import logging

from swarm.knowledge.updater import KnowledgeUpdater, UpdateEvent, dedupe_event

logger = logging.getLogger(__name__)

_updater: KnowledgeUpdater | None = None
_polling_started = False
# N-08/N-10：持引用保存后台循环 task，关闭时显式 cancel（否则 fire-and-forget 循环
# 会在已关闭的池上继续跑、并重建 updater 抢资源 → 热重启脑裂）。
_poll_task: asyncio.Task | None = None
_reprocess_task: asyncio.Task | None = None


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
        # 每 N 个轮询周期尝试补处理一次暂存的 embedding（服务恢复后自动追赶）。
        # 默认 12 周期 × 5s = 60s 一次，避免 embedding 服务持续不可用时空转刷日志。
        retry_every = 12
        cycle = 0
        while True:
            try:
                updater = await get_shared_updater()
                processed = await updater.process_pending_events()
                if processed:
                    logger.info("[KBScheduler] processed %d queued events", processed)
                cycle += 1
                if cycle % retry_every == 0:
                    recovered = await updater.retry_pending_embeddings()
                    if recovered:
                        logger.info("[KBScheduler] 补处理 %d 个暂存 embedding", recovered)
            except Exception as exc:
                logger.exception("[KBScheduler] polling error: %s", exc)
            await asyncio.sleep(interval_seconds)

    global _poll_task
    _poll_task = asyncio.create_task(_loop())
    logger.info("[KBScheduler] started (interval=%ds)", interval_seconds)


async def enqueue_kb_update(event: UpdateEvent) -> int:
    """去重后入队，返回 event id。"""
    updater = await get_shared_updater()
    return await updater.enqueue_event(dedupe_event(event))


async def shutdown_kb_scheduler() -> None:
    """关闭共享 updater + 取消后台轮询循环（测试/进程退出）。

    N-08/N-10：必须先 cancel 循环 task，否则循环会在 updater 关闭后立即重建它、
    并在已关闭的 DB 池上继续轮询抛错（热重启时还会重抢 advisory lock 致脑裂）。
    """
    global _updater, _polling_started, _poll_task, _reprocess_task
    for _t in (_poll_task, _reprocess_task):
        if _t is not None and not _t.done():
            _t.cancel()
            try:
                await _t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    _poll_task = None
    _reprocess_task = None
    if _updater is not None:
        await _updater.close()
        _updater = None
    _polling_started = False
    global _reprocess_started
    _reprocess_started = False


# ── 周期全量重预处理调度 ────────────────────────────────
_reprocess_started = False


def _is_stale(progress: dict | None, max_age_hours: float) -> bool:
    """项目预处理是否已过期（completed_at 早于 max_age_hours 前）。"""
    from datetime import datetime, timezone
    if not progress:
        return True  # 从无预处理记录 → 视为需要
    if (progress.get("phase") or "").lower() != "complete":
        return False  # 正在跑/失败的不碰（避免叠加）
    completed = progress.get("completed_at")
    if completed is None:
        return True
    if isinstance(completed, str):
        try:
            completed = datetime.fromisoformat(completed)
        except ValueError:
            return True
    now = datetime.now(timezone.utc)
    if completed.tzinfo is None:
        completed = completed.replace(tzinfo=timezone.utc)
    age_hours = (now - completed).total_seconds() / 3600.0
    return age_hours >= max_age_hours


async def start_preprocess_refresh_scheduler() -> None:
    """周期检查 stale 项目并触发全量重预处理（兜底增量更新的遗漏/漂移）。

    opt-in：SWARM_KB_AUTO_REPROCESS_HOURS>0 才启用。串行触发（一次只重跑一个，
    避免同时重跑多个项目打爆 embedding/索引）。
    """
    global _reprocess_started
    if _reprocess_started:
        return

    from swarm.config.settings import get_config
    cfg = get_config()
    max_age = float(getattr(cfg.knowledge, "auto_reprocess_hours", 0.0) or 0.0)
    if max_age <= 0:
        logger.info("[ReprocessScheduler] 未启用（auto_reprocess_hours=0）")
        return

    interval = int(getattr(cfg.knowledge, "auto_reprocess_check_interval", 1800) or 1800)
    _reprocess_started = True

    async def _loop() -> None:
        import asyncio as _aio

        from swarm.project import store as _store
        from swarm.project.preprocess import preprocess_project
        while True:
            try:
                loop = _aio.get_running_loop()
                projects = await loop.run_in_executor(None, _store.list_projects)
                for proj in projects or []:
                    pid = proj.get("id") or proj.get("project_id")
                    ppath = proj.get("path") or proj.get("repo_path")
                    if not pid or not ppath:
                        continue
                    progress = await loop.run_in_executor(None, _store.get_progress, pid)
                    if not _is_stale(progress, max_age):
                        continue
                    logger.info("[ReprocessScheduler] 项目 %s 已 stale(>%.1fh)，触发全量重预处理", pid, max_age)
                    try:
                        await preprocess_project(pid, ppath)  # 串行，跑完再下一个
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("[ReprocessScheduler] 重预处理 %s 失败: %s", pid, exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[ReprocessScheduler] loop error: %s", exc)
            await _aio.sleep(interval)

    global _reprocess_task
    _reprocess_task = asyncio.create_task(_loop())
    logger.info("[ReprocessScheduler] started (stale>%.1fh, check every %ds)", max_age, interval)
