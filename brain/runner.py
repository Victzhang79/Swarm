"""Brain 任务运行器 — 打通 create_task → Brain 执行 → interrupt → resume 主链路

职责:
- 单例 Brain graph（共享 MemorySaver，支持跨请求 resume）
- 后台执行 Brain 状态机，SSE 推送进度
- approve / revise / reject 通过 Command(resume=...) 恢复图执行
- 同步更新 task_records 状态
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any

from langgraph.types import Command

from swarm.audit import audit
from swarm.brain.graph import get_compiled_brain_graph
from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.project import store
from swarm.types import HumanDecision

logger = logging.getLogger(__name__)


class TaskTokenLimitExceeded(Exception):
    """单任务 token 估算超过 SWARM_MAX_TASK_TOKENS。"""

    def __init__(self, usage: dict[str, Any]):
        self.usage = usage
        super().__init__(f"token limit exceeded: {usage.get('total')}")

# task_id → SSE 事件队列
_task_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}

# task_id → 是否正在执行（防止重复 resume）
_task_running: set[str] = set()

# task_id → asyncio.Task 句柄（用于 cancel）
_task_handles: dict[str, asyncio.Task] = {}

# DB 中视为“进行中”的状态（API 重启后可能 orphaned）
_ACTIVE_DB_STATUSES = frozenset({
    "SUBMITTED",
    "ANALYZING",
    "PLANNING",
    "VALIDATING_PLAN",
    "CONFIRMING",
    "DISPATCHING",
    "MONITORING",
    "HANDLING_FAILURE",
    "MERGING",
    "VERIFYING_L2",
    "VERIFYING_L3",
    "DELIVERING",
    "IN_REVISION",
    "LEARNING_SUCCESS",
    "LEARNING_FAILURE",
})

# Brain 节点 → 任务状态 / UI 阶段
_NODE_STATUS_MAP: dict[str, str] = {
    "analyze": "ANALYZING",
    "plan": "PLANNING",
    "validate_plan": "VALIDATING_PLAN",
    "confirm": "CONFIRMING",
    "dispatch": "DISPATCHING",
    "monitor": "MONITORING",
    "handle_failure": "HANDLING_FAILURE",
    "merge": "MERGING",
    "verify_l2": "VERIFYING_L2",
    "verify_l3": "VERIFYING_L3",
    "deliver": "DELIVERING",
    "revision": "IN_REVISION",
    "learn_success": "LEARNING_SUCCESS",
    "learn_failure": "LEARNING_FAILURE",
}

# 需要在人工审核处暂停的 interrupt 类型
_REVIEW_INTERRUPT_TYPES = frozenset({"deliver", "confirm_plan"})


def get_task_queue(task_id: str) -> asyncio.Queue[dict[str, Any]] | None:
    return _task_queues.get(task_id)


def register_task_queue(task_id: str) -> asyncio.Queue[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _task_queues[task_id] = queue
    _cleanup_old_queues()
    return queue


def _cleanup_old_queues() -> None:
    if len(_task_queues) > 200:
        for key in list(_task_queues.keys())[: len(_task_queues) - 100]:
            if key not in _task_running:
                _task_queues.pop(key, None)


async def _emit(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    await queue.put(event)


def _set_workspace(project_id: str) -> None:
    try:
        project = store.get_project(project_id)
        if project and project.get("path"):
            os.environ["SWARM_WORKSPACE_ROOT"] = project["path"]
            logger.info("[RUNNER] workspace → %s", project["path"])
    except Exception as exc:
        logger.warning("[RUNNER] 设置 workspace 失败: %s", exc)


def _sync_task_from_state(task_id: str, state: dict[str, Any]) -> None:
    """将 Brain 状态片段写回 task_records"""
    updates: dict[str, Any] = {}

    complexity = state.get("complexity")
    if complexity is not None:
        updates["complexity"] = complexity.value if hasattr(complexity, "value") else str(complexity)

    plan = state.get("plan")
    if plan is not None:
        if hasattr(plan, "model_dump"):
            plan_dict = plan.model_dump(mode="json")
        elif isinstance(plan, dict):
            plan_dict = plan
        else:
            plan_dict = None
        if plan_dict is not None:
            updates["plan"] = plan_dict
            subtasks = plan_dict.get("subtasks") or []
            updates["subtask_count"] = len(subtasks)

    subtask_results = state.get("subtask_results")
    if isinstance(subtask_results, dict):
        updates["completed_subtasks"] = len(subtask_results)

    merged_diff = state.get("merged_diff")
    if merged_diff:
        updates["merged_diff"] = merged_diff

    merge_conflicts = state.get("merge_conflicts")
    if merge_conflicts:
        updates["merge_conflicts"] = merge_conflicts

    l3_fields: dict[str, Any] = {}
    for key in ("l3_passed", "l3_skipped", "l3_message"):
        if key in state:
            l3_fields[key] = state[key]
    if l3_fields:
        updates["l3_result"] = l3_fields

    human_decision = state.get("human_decision")
    if human_decision is not None:
        val = human_decision.value if hasattr(human_decision, "value") else str(human_decision)
        updates["human_decision"] = val.upper()

    if updates:
        try:
            store.update_task(task_id, **updates)
        except Exception as exc:
            logger.warning("[RUNNER] 同步任务状态失败 %s: %s", task_id, exc)


async def _stream_brain_events(
    task_id: str,
    graph_input: BrainState | Command,
    queue: asyncio.Queue[dict[str, Any]],
    *,
    project_id: str = "",
    module_lock: Any | None = None,
) -> tuple[dict[str, Any], Any, Any | None]:
    """执行 Brain 并流式推送节点事件，返回 (state values, snapshot)"""
    from swarm.tracing import brain_graph_config

    graph = get_compiled_brain_graph()
    task_rec = store.get_task(task_id) or {}
    thread_id = task_rec.get("thread_id") or task_id
    resume = isinstance(graph_input, Command)
    pid = project_id or task_rec.get("project_id") or ""
    config = brain_graph_config(
        task_id=task_id,
        project_id=pid,
        thread_id=thread_id,
        resume=resume,
        description=(task_rec.get("description") or "")[:200],
    )
    progress = 10

    await _emit(queue, {
        "step": "brain_invoke",
        "status": "running",
        "message": "Brain 开始编排…",
        "mode": "brain",
        "progress": progress,
    })

    async for event in graph.astream_events(graph_input, config=config, version="v2"):
        kind = event.get("event", "")
        if kind == "on_chain_start":
            name = event.get("name", "")
            if name and name not in ("LangGraph", "ChannelWrite", "increment_retry"):
                progress = min(progress + 4, 90)
                status = _NODE_STATUS_MAP.get(name)
                if status:
                    store.update_task(task_id, status=status)
                await _emit(queue, {
                    "step": "brain_node",
                    "status": "running",
                    "message": f"Brain 节点: {name}",
                    "mode": "brain",
                    "node": name,
                    "progress": progress,
                })
        elif kind == "on_chain_end":
            name = event.get("name", "")
            output = (event.get("data") or {}).get("output") or {}
            if name in ("analyze", "plan", "merge", "verify_l3", "dispatch") and isinstance(output, dict):
                _sync_task_from_state(task_id, output)
            if name in ("merge", "dispatch") and isinstance(output, dict):
                fresh = store.get_task(task_id) or task_rec
                ok, usage = store.check_task_token_limit(
                    task_id,
                    description=fresh.get("description") or "",
                    merged_diff=output.get("merged_diff") or fresh.get("merged_diff") or "",
                    subtask_results=output.get("subtask_results"),
                )
                if not ok:
                    await _emit(queue, {
                        "step": "token_limit",
                        "status": "failed",
                        "message": (
                            f"单任务 token 估算超限 ({usage.get('total')}/"
                            f"{get_config().max_task_tokens})"
                        ),
                        "mode": "brain",
                        "progress": 100,
                    })
                    raise TaskTokenLimitExceeded(usage)
            if name == "analyze":
                if isinstance(output, dict):
                    kc = output.get("knowledge_context") or {}
                    complexity = output.get("complexity")
                    if hasattr(complexity, "value"):
                        complexity = complexity.value
                    stats = {
                        "struct_count": len(kc.get("struct") or []),
                        "semantic_count": len(kc.get("semantic") or []),
                        "norms_count": len(kc.get("norms") or []),
                        "mistakes_count": len(kc.get("mistakes") or []),
                        "successes_count": len(kc.get("successes") or []),
                    }
                    await _emit(queue, {
                        "step": "knowledge_retrieved",
                        "status": "done",
                        "node": "analyze",
                        "complexity": str(complexity) if complexity else None,
                        "knowledge_stats": stats,
                        "message": (
                            f"知识检索: Harness {stats['norms_count']} · "
                            f"符号 {stats['struct_count']} · "
                            f"错题 {stats['mistakes_count']}"
                        ),
                        "mode": "brain",
                        "progress": progress,
                    })
                    await _emit(queue, {
                        "step": "brain_node",
                        "status": "done",
                        "node": "analyze",
                        "mode": "brain",
                        "progress": progress,
                    })
            if name == "plan" and module_lock is not None and isinstance(output, dict):
                plan_obj = output.get("plan")
                if plan_obj is not None:
                    from swarm.infra.redis_client import upgrade_module_lock

                    if hasattr(plan_obj, "model_dump"):
                        plan_dict = plan_obj.model_dump(mode="json")
                    elif isinstance(plan_obj, dict):
                        plan_dict = plan_obj
                    else:
                        plan_dict = None
                    if plan_dict is not None:
                        module_lock = upgrade_module_lock(module_lock, pid, plan_dict)

    snapshot = await graph.aget_state(config)
    final_state = dict(snapshot.values) if snapshot and snapshot.values else {}
    return final_state, snapshot, module_lock


def _extract_interrupt_info(snapshot: Any, state: dict[str, Any]) -> dict[str, Any] | None:
    """从 LangGraph snapshot 或 state 中提取 interrupt 载荷"""
    interrupts = getattr(snapshot, "interrupts", None) if snapshot is not None else None
    if interrupts:
        payload = interrupts[0]
        val = payload.value if hasattr(payload, "value") else payload
        if isinstance(val, dict):
            return val
        return {"type": str(val)}

    # 兼容 invoke 返回值
    legacy = state.get("__interrupt__")
    if legacy:
        if isinstance(legacy, list) and legacy:
            payload = legacy[0]
            val = payload.value if hasattr(payload, "value") else payload
            if isinstance(val, dict):
                return val
    return None


async def _handle_post_run(
    task_id: str,
    state: dict[str, Any],
    queue: asyncio.Queue[dict[str, Any]],
    snapshot: Any = None,
) -> None:
    """运行结束后：同步 DB、判断是否在 interrupt 等待人工"""
    _sync_task_from_state(task_id, state)

    interrupt_info = _extract_interrupt_info(snapshot, state)
    if interrupt_info:
        interrupt_type = interrupt_info.get("type", "")
        if interrupt_type in _REVIEW_INTERRUPT_TYPES:
            status = "CONFIRMING" if interrupt_type == "confirm_plan" else "DELIVERING"
            store.update_task(task_id, status=status)
            label = "计划确认" if interrupt_type == "confirm_plan" else "结果审核"
            await _emit(queue, {
                "step": "awaiting_review",
                "status": "waiting",
                "message": f"⏸ 等待人工{label}",
                "mode": "brain",
                "interrupt_type": interrupt_type,
                "interrupt": interrupt_info,
                "progress": 95,
            })
            return

    # 正常结束
    task_rec = store.get_task(task_id) or {}
    token_usage = store.estimate_token_usage(
        description=task_rec.get("description") or state.get("task_description") or "",
        merged_diff=state.get("merged_diff") or "",
        subtask_results=state.get("subtask_results"),
    )
    duration = store.compute_task_duration_seconds(task_rec)
    store.update_task(
        task_id,
        status="DONE",
        token_usage=token_usage,
        duration_seconds=round(duration, 2) if duration is not None else None,
    )
    output_parts = _build_result_payload(state)
    await _emit(queue, {
        "step": "complete",
        "status": "done",
        "message": "任务执行完成",
        "mode": "brain",
        "progress": 100,
    })
    await _emit(queue, {"step": "result", "mode": "brain", "result": output_parts})


def _build_result_payload(state: dict[str, Any]) -> dict[str, Any]:
    output_parts: dict[str, Any] = {}
    for key in ("merged_diff", "l2_passed", "learn_summary", "complexity", "plan", "subtask_results", "human_decision", "learned", "knowledge_context", "merge_conflicts", "l3_passed", "l3_skipped", "l3_message", "plan_validation_issues", "shared_contract", "verification_failure"):
        val = state.get(key)
        if val is None or val == "" or val == {}:
            continue
        if hasattr(val, "model_dump"):
            output_parts[key] = val.model_dump(mode="json")
        elif isinstance(val, dict):
            output_parts[key] = val
        else:
            output_parts[key] = str(val) if not isinstance(val, (bool, int, float)) else val
    return output_parts


async def run_task(
    task_id: str,
    project_id: str,
    description: str,
    auto_accept: bool | None = None,
) -> None:
    """后台启动 Brain 任务（从 SUBMITTED 到 DONE 或 interrupt）"""
    queue = register_task_queue(task_id)
    if task_id in _task_running:
        await _emit(queue, {"step": "error", "status": "error", "message": "任务已在执行中"})
        return

    _task_running.add(task_id)
    _set_workspace(project_id)

    from swarm.infra.redis_client import ModuleLock, TaskQueue

    TaskQueue.enqueue(task_id, project_id)
    module_lock = ModuleLock(project_id, "default")
    if not module_lock.acquire():
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": "同项目模块锁被占用，请稍后重试",
        })
        _task_running.discard(task_id)
        return

    if auto_accept is None:
        auto_accept = os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")

    task_rec = store.get_task(task_id) or {}
    user_id = task_rec.get("created_by_user_id") or ""
    from swarm.memory.profile import load_profile_prompts

    profile, brain_prompt, worker_prompt = load_profile_prompts(
        user_id or None,
        project_id,
    )

    initial_state: BrainState = {
        "task_id": task_id,
        "task_description": description,
        "project_id": project_id,
        "user_id": user_id,
        "user_profile": profile,
        "user_profile_prompt_brain": brain_prompt,
        "user_profile_prompt_worker": worker_prompt,
        "auto_accept": auto_accept,
    }

    project_path = None
    try:
        proj = store.get_project(project_id)
        if proj:
            project_path = proj.get("path")
    except Exception as exc:
        logger.debug("获取项目路径失败 project_id=%s: %s", project_id, exc)
    from swarm.memory.session import build_session_metadata

    initial_state["session_metadata"] = build_session_metadata(
        project_path=project_path,
        client="api",
    )

    try:
        thread_id = task_rec.get("thread_id") or task_id
        store.update_task(task_id, status="ANALYZING", thread_id=thread_id)
        audit(
            "task_start",
            orchestrator="Brain",
            task_id=task_id,
            project_id=project_id,
            description=description[:200],
        )
        state, snapshot, module_lock = await _stream_brain_events(
            task_id, initial_state, queue, project_id=project_id, module_lock=module_lock,
        )
        await _handle_post_run(task_id, state, queue, snapshot)
        audit(
            "task_complete",
            orchestrator="Brain",
            task_id=task_id,
            project_id=project_id,
            status=store.get_task(task_id).get("status") if store.get_task(task_id) else "UNKNOWN",
        )
    except asyncio.CancelledError:
        logger.info("[RUNNER] 任务 %s 已取消", task_id)
        store.update_task(task_id, status="CANCELLED")
        audit("task_cancelled", orchestrator="Brain", task_id=task_id, project_id=project_id)
        await _emit(queue, {
            "step": "cancelled",
            "status": "cancelled",
            "message": "任务已取消",
            "progress": -1,
        })
    except Exception as exc:
        logger.exception("[RUNNER] 任务 %s 执行失败", task_id)
        store.update_task(task_id, status="FAILED")
        audit("task_failed", orchestrator="Brain", task_id=task_id, project_id=project_id, error=str(exc)[:300])
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": f"执行失败: {exc}",
            "progress": -1,
        })
    finally:
        module_lock.release()
        _task_running.discard(task_id)


async def resume_task(
    task_id: str,
    decision: str,
    feedback: str = "",
) -> None:
    """恢复被 interrupt 暂停的任务"""
    queue = _task_queues.get(task_id) or register_task_queue(task_id)

    if task_id in _task_running:
        await _emit(queue, {"step": "error", "status": "error", "message": "任务正在执行，请稍候"})
        return

    task = store.get_task(task_id)
    if not task:
        await _emit(queue, {"step": "error", "status": "error", "message": "任务不存在"})
        return

    _task_running.add(task_id)
    _set_workspace(task["project_id"])

    decision_norm = decision.lower().strip()
    if decision_norm in ("approved", "approve", "accept"):
        decision_norm = HumanDecision.ACCEPT.value
    elif decision_norm in ("revised", "revise"):
        decision_norm = HumanDecision.REVISE.value
    elif decision_norm in ("rejected", "reject"):
        decision_norm = HumanDecision.REJECT.value

    store.update_task(
        task_id,
        human_decision=decision_norm.upper(),
        status="IN_REVISION" if decision_norm == HumanDecision.REVISE.value else "ANALYZING",
    )

    resume_payload: dict[str, Any] = {"decision": decision_norm, "feedback": feedback}

    try:
        await _emit(queue, {
            "step": "resume",
            "status": "running",
            "message": f"恢复执行: {decision_norm}",
            "mode": "brain",
            "progress": 50,
        })
        state, snapshot, _lock = await _stream_brain_events(
            task_id,
            Command(resume=resume_payload),
            queue,
            project_id=task.get("project_id", ""),
        )
        await _handle_post_run(task_id, state, queue, snapshot)
    except Exception as exc:
        logger.exception("[RUNNER] 任务 %s resume 失败", task_id)
        store.update_task(task_id, status="FAILED")
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": f"恢复失败: {exc}",
            "progress": -1,
        })
    finally:
        _task_running.discard(task_id)


def is_task_running(task_id: str) -> bool:
    return task_id in _task_running


def is_task_orphaned(task_id: str) -> bool:
    """DB 为活跃状态但本进程未在跑（常见于 API 重启后）"""
    task = store.get_task(task_id)
    if not task:
        return False
    status = task.get("status", "")
    return status in _ACTIVE_DB_STATUSES and task_id not in _task_running


def can_retry_task(task_id: str) -> tuple[bool, str]:
    """是否允许重跑任务"""
    task = store.get_task(task_id)
    if not task:
        return False, "任务不存在"

    if task_id in _task_running:
        return False, "任务正在执行中"

    if is_task_orphaned(task_id):
        return True, ""

    status = task.get("status", "")
    if status in ("FAILED", "CANCELLED", "DONE"):
        return True, ""

    if status in ("DELIVERING", "CONFIRMING"):
        return False, "任务等待人工审核，请先通过/修订/拒绝"

    if status in _ACTIVE_DB_STATUSES:
        return False, "任务仍在执行中"

    return False, f"当前状态 {status} 不可重跑"


async def cancel_task(task_id: str) -> bool:
    """取消正在运行的任务，或将 orphaned 活跃任务标记为 CANCELLED"""
    task = store.get_task(task_id)
    if not task:
        return False

    handle = _task_handles.get(task_id)
    if handle and not handle.done():
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass

    _task_running.discard(task_id)

    queue = _task_queues.get(task_id)
    if queue:
        await _emit(queue, {
            "step": "cancelled",
            "status": "cancelled",
            "message": "任务已取消",
            "progress": -1,
        })

    if task.get("status") != "CANCELLED":
        store.update_task(task_id, status="CANCELLED")
    return True


async def retry_task(task_id: str, auto_accept: bool | None = None) -> bool:
    """重置任务字段并重新执行"""
    allowed, reason = can_retry_task(task_id)
    if not allowed:
        logger.warning("[RUNNER] 任务 %s 不可重跑: %s", task_id, reason)
        return False

    task = store.get_task(task_id)
    if not task:
        return False

    if task_id in _task_running:
        await cancel_task(task_id)

    new_thread_id = f"{task_id}-r-{secrets.token_hex(4)}"
    store.update_task(
        task_id,
        status="SUBMITTED",
        plan={},
        merged_diff="",
        subtask_count=0,
        completed_subtasks=0,
        human_decision="",
        thread_id=new_thread_id,
    )

    await run_task(
        task_id,
        task["project_id"],
        task["description"],
        auto_accept=auto_accept,
    )
    return True


def start_task_background(
    task_id: str,
    project_id: str,
    description: str,
    auto_accept: bool = False,
) -> None:
    """在 FastAPI 后台启动任务（非阻塞）"""
    register_task_queue(task_id)

    async def _wrap() -> None:
        try:
            await run_task(task_id, project_id, description, auto_accept=auto_accept)
        finally:
            _task_handles.pop(task_id, None)

    _task_handles[task_id] = asyncio.create_task(_wrap())


def resume_task_background(task_id: str, decision: str, feedback: str = "") -> None:
    """在 FastAPI 后台 resume 任务"""
    async def _wrap() -> None:
        try:
            await resume_task(task_id, decision, feedback)
        finally:
            _task_handles.pop(task_id, None)

    _task_handles[task_id] = asyncio.create_task(_wrap())


def cancel_task_background(task_id: str) -> None:
    """在 FastAPI 后台取消任务"""
    asyncio.create_task(cancel_task(task_id))


def retry_task_background(task_id: str, auto_accept: bool | None = None) -> None:
    """在 FastAPI 后台重跑任务"""
    register_task_queue(task_id)

    async def _wrap() -> None:
        try:
            await retry_task(task_id, auto_accept=auto_accept)
        finally:
            _task_handles.pop(task_id, None)

    _task_handles[task_id] = asyncio.create_task(_wrap())
