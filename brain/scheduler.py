"""任务准入调度器 — 让优先级队列真正生效（进程内有界并发）。

设计说明（架构决策）:
    当前 Brain 在 API 进程内通过 asyncio 执行（非独立 worker 进程）。本调度器
    在此模型下引入"准入控制"：create_task 不再直接 fire Brain，而是入优先级队列，
    由后台消费循环按 max_concurrent 上限取任务执行。

    效果:
    - urgent > normal > background 优先级真正生效（高优先级任务插队）
    - 全局并发上限（SWARM_MAX_CONCURRENT_TASKS，默认取 worker.max_concurrent）
    - 队列积压可观测（pending_count）

    边界:
    - 仍是单进程模型。多 API 副本需要外置队列（见 README Roadmap）。
    - resume（审核后恢复）不走队列，直接执行（已在审核态，不占新并发额度）。
"""

from __future__ import annotations

import asyncio
import logging
import os

from swarm.config.settings import get_config
from swarm.infra.redis_client import TaskQueue

logger = logging.getLogger(__name__)

# 任务描述缓存：task_id → (project_id, description, auto_accept)
# 队列只存 task_id/project_id，执行参数在此补全（避免 Redis payload 过大）
_pending_meta: dict[str, dict] = {}

_consumer_started = False
_inflight: set[str] = set()
_wakeup: asyncio.Event | None = None


def _max_concurrent() -> int:
    raw = os.environ.get("SWARM_MAX_CONCURRENT_TASKS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, get_config().worker.max_concurrent)


def submit_task(
    task_id: str,
    project_id: str,
    description: str,
    *,
    auto_accept: bool = False,
    priority: str = "normal",
) -> None:
    """提交任务到优先级队列（准入控制，不立即执行）。"""
    _pending_meta[task_id] = {
        "project_id": project_id,
        "description": description,
        "auto_accept": auto_accept,
    }
    TaskQueue.enqueue(task_id, project_id, priority=priority)
    logger.info("[Scheduler] 任务入队 task=%s priority=%s", task_id, priority)
    if _wakeup is not None:
        _wakeup.set()


def pending_count() -> int:
    """当前队列积压 + 在执行任务数（监控用）。"""
    return len(_pending_meta) + len(_inflight)


async def start_task_scheduler() -> None:
    """启动后台消费循环（API startup 调用，幂等）。"""
    global _consumer_started, _wakeup
    if _consumer_started:
        return
    _consumer_started = True
    _wakeup = asyncio.Event()

    async def _loop() -> None:
        from swarm.brain.runner import start_task_background

        while True:
            # 并发未满则尝试出队
            if len(_inflight) < _max_concurrent():
                item = TaskQueue.dequeue()
                if item:
                    task_id = item["task_id"]
                    meta = _pending_meta.pop(task_id, None)
                    if meta is None:
                        # 元数据丢失（如进程重启）→ 跳过，由 orphan 恢复逻辑处理
                        logger.warning("[Scheduler] 任务 %s 缺执行元数据，跳过", task_id)
                        continue
                    _inflight.add(task_id)
                    _run_with_slot(task_id, meta, start_task_background)
                    continue  # 立即尝试下一个（填满并发额度）
            # 队列空或并发已满 → 等唤醒或轮询
            try:
                if _wakeup is not None:
                    await asyncio.wait_for(_wakeup.wait(), timeout=2.0)
                    _wakeup.clear()
            except asyncio.TimeoutError:
                pass

    asyncio.create_task(_loop())
    logger.info("[Scheduler] 任务准入调度器已启动 (max_concurrent=%d)", _max_concurrent())


def _run_with_slot(task_id: str, meta: dict, start_fn) -> None:
    """执行任务并在完成时释放并发额度。"""
    import asyncio as _asyncio

    async def _wrap() -> None:
        try:
            # start_task_background 内部 create_task 异步执行；这里要等它真正跑完
            # 才能释放额度，所以直接 await run_task 逻辑的包装。
            from swarm.brain.runner import run_task

            await run_task(
                task_id,
                meta["project_id"],
                meta["description"],
                auto_accept=meta["auto_accept"],
            )
        except Exception as exc:
            logger.exception("[Scheduler] 任务执行异常 task=%s: %s", task_id, exc)
        finally:
            _inflight.discard(task_id)
            if _wakeup is not None:
                _wakeup.set()  # 释放额度 → 唤醒消费循环取下一个

    # 注册 SSE 队列（与原 start_task_background 行为一致）
    from swarm.brain.runner import register_task_queue

    register_task_queue(task_id)
    _asyncio.create_task(_wrap())
