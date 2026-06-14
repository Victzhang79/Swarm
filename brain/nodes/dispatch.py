"""brain/nodes/dispatch.py — dispatch/monitor 节点 + 安全审计（B1 批3 抽出）。

被测试 patch 的 _dispatch_to_worker 留在 __init__.py；本模块内对它的调用用
`nodes._dispatch_to_worker(...)` 模块限定，使 patch("swarm.brain.nodes._dispatch_to_worker") 命中。
"""

from __future__ import annotations

import asyncio
import logging

from swarm.audit import audit
from swarm.brain import nodes
from swarm.brain.context_log import touch_context
from swarm.brain.nodes.shared import _diff_has_changes, _worker_profile_prompt
from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.memory.sliding_window import PRIORITY_WORKER
from swarm.types import Confidence, SubTask, WorkerOutput

logger = logging.getLogger(__name__)


async def dispatch(state: BrainState) -> dict:
    """DISPATCH 节点 — 将就绪的子任务派发给 Worker

    输入: plan, dispatch_remaining, subtask_results, knowledge_context
    输出: subtask_results, dispatch_remaining
    """
    plan_obj = state.get("plan")
    if plan_obj is None:
        logger.error("[DISPATCH] 没有执行计划")
        return {"dispatch_remaining": []}

    subtask_results: dict = state.get("subtask_results", {})
    dispatch_remaining: list = state.get("dispatch_remaining", [])
    knowledge_context = state.get("knowledge_context", {})

    # 如果是首次进入 dispatch，初始化 dispatch_remaining
    if not dispatch_remaining and not subtask_results:
        dispatch_remaining = [t.id for t in plan_obj.subtasks]

    # audit #19：重入防护——dispatch_remaining 为空但仍有"既未完成、也不在 remaining"
    # 的子任务时（理论上不该出现，但 handle_failure/rebase 等异常路径可能造成），
    # 把这些遗漏子任务补回 remaining，避免直接跳过派发导致任务卡死/漏做。
    _completed = set(subtask_results.keys())
    if not dispatch_remaining:
        _orphaned = [t.id for t in plan_obj.subtasks if t.id not in _completed]
        if _orphaned:
            logger.warning(
                "[DISPATCH] 检测到 %d 个未完成但不在 remaining 的子任务，补回派发队列: %s",
                len(_orphaned), _orphaned,
            )
            dispatch_remaining = _orphaned

    completed_ids = set(subtask_results.keys())
    config = get_config()
    max_concurrent = config.worker.max_concurrent

    to_dispatch = plan_obj.get_dispatch_batch(
        completed_ids, dispatch_remaining, max_concurrent
    )

    logger.info(
        f"[DISPATCH] 派发 {len(to_dispatch)} 个子任务（并行批次） "
        f"(已完成={len(completed_ids)}, 剩余={len(dispatch_remaining)})"
    )

    if not to_dispatch:
        return {
            "subtask_results": subtask_results,
            "dispatch_remaining": dispatch_remaining,
        }

    project_id = state.get("project_id", "")
    task_id = state.get("task_id", "")

    # 注：原先这里调用 SandboxPool(...).warmup(project_id) 做"预热"，但那是
    # 失效死代码——每次都 new 一个临时 SandboxPool，warmup 把沙箱塞进它的 _pool
    # 后实例即被 GC，远端沙箱却永不回收 → 每次 dispatch 必产生 1 个孤儿沙箱。
    # 而真正的 worker 走 executor 的 create 路径，从不 acquire 这个池。
    # 预热既无收益又泄漏，直接移除。如需预热，应由长生命周期的单例池统一管理。

    use_alternate = bool(state.get("use_alternate_model", False))
    shared_contract = state.get("shared_contract") or (
        plan_obj.shared_contract if plan_obj else {}
    )

    async def _run_one(subtask: SubTask) -> tuple[SubTask, WorkerOutput | Exception]:
        try:
            output = await nodes._dispatch_to_worker(
                subtask,
                knowledge_context,
                project_id=project_id,
                task_id=task_id,
                use_alternate=use_alternate,
                user_profile_prompt=_worker_profile_prompt(state),
                shared_contract=shared_contract,
            )
            return subtask, output
        except Exception as e:
            return subtask, e

    outcomes = await asyncio.gather(*[_run_one(st) for st in to_dispatch])

    def _worker_batch_context() -> dict:
        lines: list[str] = []
        for st, oc in outcomes:
            if isinstance(oc, WorkerOutput):
                summary = (oc.summary or "")[:120]
                l1 = "通过" if oc.l1_passed else "未通过"
                lines.append(f"{st.id}: {summary} (L1={l1}, diff={len(oc.diff or '')} chars)")
            elif isinstance(oc, Exception):
                lines.append(f"{st.id}: 执行异常 — {str(oc)[:100]}")
        if not lines:
            return {}
        return touch_context(
            state,
            "worker_batch",
            "\n".join(lines),
            priority=PRIORITY_WORKER,
        )

    worker_ctx = _worker_batch_context()

    # 收集整批结果 —— 不再遇到首个失败就 return，避免丢弃同批已完成的兄弟结果
    failed_ids = list(state.get("failed_subtask_ids", []))
    for subtask, outcome in outcomes:
        if isinstance(outcome, Exception):
            logger.error(f"[DISPATCH] 子任务 {subtask.id} 执行失败: {outcome}")
            subtask_results[subtask.id] = WorkerOutput(
                subtask_id=subtask.id,
                diff="",
                summary=f"执行失败: {outcome}",
                confidence=Confidence.LOW,
                l1_passed=False,
                l1_details={"error": str(outcome)},
            )
            if subtask.id not in failed_ids:
                failed_ids.append(subtask.id)
            if subtask.id in dispatch_remaining:
                dispatch_remaining.remove(subtask.id)
            continue

        worker_output = outcome
        subtask_results[subtask.id] = worker_output
        if subtask.id in dispatch_remaining:
            dispatch_remaining.remove(subtask.id)
        logger.info(
            f"[DISPATCH] 子任务 {subtask.id} 完成 "
            f"(L1={'通过' if worker_output.l1_passed else '未通过'}, "
            f"diff={len(worker_output.diff or '')} chars)"
        )
        if not _diff_has_changes(worker_output.diff or "") or not worker_output.l1_passed:
            if subtask.id not in failed_ids:
                failed_ids.append(subtask.id)

    result: dict = {
        "subtask_results": subtask_results,
        "dispatch_remaining": dispatch_remaining,
        **worker_ctx,
    }
    if failed_ids:
        result["failed_subtask_ids"] = failed_ids
    return result


def monitor(state: BrainState) -> dict:
    """MONITOR 节点 — 监控执行进度，检查是否还有下游/有无失败

    输入: dispatch_remaining, subtask_results, failed_subtask_ids
    输出: 无状态变更，仅作为路由判断节点
    """
    dispatch_remaining = state.get("dispatch_remaining", [])
    subtask_results: dict = state.get("subtask_results", {})
    failed_ids = state.get("failed_subtask_ids", [])

    logger.info(
        f"[MONITOR] 剩余={len(dispatch_remaining)}, "
        f"已完成={len(subtask_results)}, 失败={len(failed_ids)}"
    )

    # 此节点不做状态变更，仅用于条件路由
    return {}
