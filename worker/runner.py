"""Standalone Worker 运行器 — Phase 0 单 Worker 验证（不经 Brain）

提供 SSE 队列，供 API / UI 直接跑 WorkerExecutor。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from swarm.project import store
from swarm.types import FileScope, SubTask, SubTaskDifficulty, WorkerOutput

logger = logging.getLogger(__name__)

_worker_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
_worker_running: set[str] = set()


def get_worker_queue(run_id: str) -> asyncio.Queue[dict[str, Any]] | None:
    return _worker_queues.get(run_id)


def register_worker_queue(run_id: str) -> asyncio.Queue[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _worker_queues[run_id] = queue
    return queue


def is_worker_running(run_id: str) -> bool:
    return run_id in _worker_running


async def _emit(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    await queue.put(event)


def _set_workspace(project_id: str) -> str | None:
    project = store.get_project(project_id)
    if project and project.get("path"):
        os.environ["SWARM_WORKSPACE_ROOT"] = project["path"]
        return project["path"]
    return None


async def run_standalone_worker(
    run_id: str,
    project_id: str,
    description: str,
    *,
    difficulty: str = "medium",
    writable: list[str] | None = None,
    readable: list[str] | None = None,
) -> None:
    """后台执行单 Worker（无 Brain 拆解）。"""
    queue = _worker_queues.get(run_id) or register_worker_queue(run_id)
    if run_id in _worker_running:
        await _emit(queue, {"step": "error", "status": "error", "message": "Worker 已在运行"})
        return

    _worker_running.add(run_id)
    project_path = _set_workspace(project_id)
    if not project_path:
        await _emit(queue, {"step": "error", "status": "error", "message": f"项目不存在: {project_id}"})
        _worker_running.discard(run_id)
        return

    diff_enum = SubTaskDifficulty.MEDIUM
    try:
        diff_enum = SubTaskDifficulty(difficulty.lower())
    except ValueError:
        pass

    # 空字符串 scope 项表示全项目可读写（FileScope.endswith 规则）
    w = writable if writable else [""]
    r = readable if readable else [""]
    scope = FileScope(writable=w, readable=r)
    subtask = SubTask(
        id=run_id,
        description=description,
        difficulty=diff_enum,
        scope=scope,
    )

    from swarm.worker.executor import WorkerExecutor

    executor = WorkerExecutor(
        subtask=subtask,
        scope=scope,
        project_id=project_id,
        project_path=project_path,
    )

    log_task: asyncio.Task | None = None

    async def _stream_logs() -> None:
        last = 0
        while run_id in _worker_running:
            logs = executor.execution_log
            if len(logs) > last:
                for line in logs[last:]:
                    await _emit(queue, {
                        "step": "log",
                        "status": "running",
                        "message": line,
                        "phase": executor.phase.value,
                    })
                last = len(logs)
            await asyncio.sleep(0.5)

    try:
        await _emit(queue, {
            "step": "start",
            "status": "running",
            "message": "Standalone Worker 启动",
            "mode": "worker",
            "project_id": project_id,
        })
        log_task = asyncio.create_task(_stream_logs())
        output: WorkerOutput = await executor.run()
        await _emit(queue, {
            "step": "result",
            "status": "done",
            "mode": "worker",
            "result": output.model_dump(mode="json"),
        })
        await _emit(queue, {
            "step": "complete",
            "status": "done",
            "message": "Worker 执行完成",
            "mode": "worker",
            "progress": 100,
        })
    except Exception as exc:
        logger.exception("[WORKER] standalone %s failed", run_id)
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": str(exc),
            "mode": "worker",
        })
    finally:
        _worker_running.discard(run_id)
        if log_task:
            log_task.cancel()
            try:
                await log_task
            except asyncio.CancelledError:
                pass


def start_standalone_worker_background(
    run_id: str,
    project_id: str,
    description: str,
    **kwargs: Any,
) -> None:
    register_worker_queue(run_id)
    asyncio.create_task(
        run_standalone_worker(run_id, project_id, description, **kwargs)
    )
