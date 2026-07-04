"""worker 派发抽象 — B2 地基（服务边界可热拔插）。

当前 Brain 编排在 `_dispatch_to_worker` 里**同进程**直接 `WorkerExecutor(...).run()`。
B2 目标是把 Worker 执行拆成独立进程/容器（与 Docker 多容器交付合并落地）。

本模块把"派发一个子任务给 Worker 执行"抽象成 `WorkerDispatcher` 接口：
- `InProcessDispatcher`（默认）：同进程跑 WorkerExecutor —— 行为与拆分前**完全一致**，
  单机/当前部署零变化。
- `QueueDispatcher`（预留，Docker 化时落地）：投递子任务到 PG 任务队列，独立 Worker
  容器拉取执行，结果回写 PG。届时只需实现本接口 + 切 env，Brain 编排代码不改。

延续 A1 的"留地基/可热拔插"范式（CoordinationBackend / SandboxPoolStrategy / SchedulerLeadership）。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from swarm.types import SubTask, WorkerOutput


class WorkerDispatcher(ABC):
    """把一个子任务派发给 Worker 执行，返回 WorkerOutput。

    实现负责"怎么把活送到 Worker 并拿回结果"——同进程直跑 / 投队列等独立进程拉。
    入参与原 `_dispatch_to_worker` 内 WorkerExecutor 构造一致，保证替换零语义差。
    """

    @abstractmethod
    async def dispatch(
        self,
        subtask: SubTask,
        *,
        model_name: str | None,
        knowledge: Any,
        project_id: str | None,
        project_path: str | None,
        task_id: str | None,
        user_profile_prompt: str,
        shared_contract: dict | None,
        recursion_boost: int = 0,
        base_ref: str | None = None,
    ) -> WorkerOutput:
        ...


class InProcessDispatcher(WorkerDispatcher):
    """默认实现：同进程内构造 WorkerExecutor 并 await run()。

    与 B2 拆分前的行为**逐字节一致**（搬运自原 _dispatch_to_worker）。
    """

    async def dispatch(
        self,
        subtask: SubTask,
        *,
        model_name: str | None,
        knowledge: Any,
        project_id: str | None,
        project_path: str | None,
        task_id: str | None,
        user_profile_prompt: str,
        shared_contract: dict | None,
        recursion_boost: int = 0,
        base_ref: str | None = None,
    ) -> WorkerOutput:
        from swarm.worker.executor import WorkerExecutor

        executor = WorkerExecutor(
            subtask=subtask,
            model_name=model_name if isinstance(model_name, str) else None,
            knowledge=knowledge,
            project_id=project_id or None,
            project_path=project_path,
            task_id=task_id or None,
            user_profile_prompt=user_profile_prompt,
            shared_contract=shared_contract or {},
            recursion_boost=recursion_boost,
            base_ref=base_ref,
        )
        return await executor.run()


# 进程级单例（无状态，可安全复用）
_dispatcher: WorkerDispatcher | None = None


def get_worker_dispatcher() -> WorkerDispatcher:
    """工厂：按 SWARM_WORKER_DISPATCH_MODE 选择派发实现。

    - 未设置 / "inprocess"（默认）：InProcessDispatcher（当前行为，单机零变化）
    - "queue"（预留）：QueueDispatcher —— Docker 多容器化时落地，当前未实现则回退 inprocess
      并告警（开箱即用不被破坏）。
    """
    global _dispatcher
    if _dispatcher is not None:
        return _dispatcher

    mode = os.environ.get("SWARM_WORKER_DISPATCH_MODE", "inprocess").strip().lower()
    if mode == "queue":
        # 预留：Docker 化时实现 QueueDispatcher（PG 队列）。当前未实现 → 回退 inprocess。
        import logging
        logging.getLogger(__name__).warning(
            "SWARM_WORKER_DISPATCH_MODE=queue 尚未实现（B2/Docker 阶段落地），回退 inprocess"
        )
        _dispatcher = InProcessDispatcher()
    else:
        _dispatcher = InProcessDispatcher()
    return _dispatcher


def reset_worker_dispatcher() -> None:
    """测试辅助：重置单例（便于 patch 不同实现）。"""
    global _dispatcher
    _dispatcher = None
