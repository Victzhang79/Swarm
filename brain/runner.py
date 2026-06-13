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
_REVIEW_INTERRUPT_TYPES = frozenset({"deliver", "confirm_plan", "clarify", "review_design"})

# interrupt 类型 → (任务状态, 人类可读标签)
_INTERRUPT_STATUS_LABEL = {
    "confirm_plan": ("CONFIRMING", "计划确认"),
    "deliver": ("DELIVERING", "结果审核"),
    "clarify": ("CLARIFYING", "需求澄清"),
    "review_design": ("DESIGN_REVIEW", "技术方案评审"),
}


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


def _emit_task_notification(task_id: str, task_rec: dict[str, Any], status: str) -> None:
    """写入应用内通知（任务完成/失败，带 task_id）。失败不影响主流程。"""
    try:
        desc = (task_rec.get("description") or "")[:80]
        if status == "DONE":
            title, etype = "任务已完成", "task_completed"
        elif status == "FAILED":
            title, etype = "任务失败", "task_failed"
        else:
            title, etype = "任务更新", "task_updated"
        store.create_notification(
            etype,
            task_id=task_id,
            project_id=task_rec.get("project_id"),
            title=title,
            message=f"#{task_id[:8]} {desc}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[RUNNER] 写通知失败: %s", exc)


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
            status, label = _INTERRUPT_STATUS_LABEL.get(interrupt_type, ("DELIVERING", "结果审核"))
            store.update_task(task_id, status=status)
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
    _emit_task_notification(task_id, task_rec, "DONE")
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
    # 绑定 task 上下文：本协程（asyncio.Task）内所有 swarm 日志自动带 [task=...]。
    # 放在 run_task 内可覆盖所有入口（调度器准入 / 后台 / 直接调用）。
    from swarm.logging_config import set_task_context

    set_task_context(task_id)
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

    # B 部分：透传上传文件给摄取节点（存在 task_records.uploaded_files）。
    # 无文件时 ingest 节点 no-op 直通，对纯文字任务零影响。
    _uploaded = task_rec.get("uploaded_files") or []
    if _uploaded:
        initial_state["uploaded_files"] = list(_uploaded)
    if task_rec.get("auto_confirm_vision"):
        initial_state["auto_confirm_vision"] = True

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
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
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
        # 兜底：释放本任务残留的沙箱（正常路径 worker 已自清，此处防漏）
        try:
            from swarm.worker.sandbox import get_sandbox_manager

            get_sandbox_manager().kill_by_task(task_id)
        except Exception:
            pass


async def resume_task(
    task_id: str,
    decision: str,
    feedback: str = "",
) -> None:
    """恢复被 interrupt 暂停的任务"""
    from swarm.logging_config import set_task_context

    set_task_context(task_id)
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
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": f"恢复失败: {exc}",
            "progress": -1,
        })
    finally:
        _task_running.discard(task_id)


async def resume_planning(task_id: str, payload: dict[str, Any]) -> None:
    """恢复被规划子图 interrupt（clarify / review_design）暂停的任务。

    与 resume_task 区别：clarify/review 的 resume 是结构化 payload（透传原样给 graph），
    不走 ACCEPT/REVISE/REJECT 那套人工决策归一化。
    - clarify：payload = {q_index: answer, ...} 或 {"action": "skip"}
    - review_design：payload = {"decision": "approve"|"reject", "feedback": "..."}
    """
    from swarm.logging_config import set_task_context

    set_task_context(task_id)

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
    store.update_task(task_id, status="ANALYZING")
    try:
        await _emit(queue, {
            "step": "resume", "status": "running",
            "message": "恢复规划（澄清/方案评审已提交）", "mode": "brain", "progress": 30,
        })
        state, snapshot, _lock = await _stream_brain_events(
            task_id,
            Command(resume=payload),
            queue,
            project_id=task.get("project_id", ""),
        )
        await _handle_post_run(task_id, state, queue, snapshot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[RUNNER] 任务 %s 规划 resume 失败", task_id)
        store.update_task(task_id, status="FAILED")
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
        await _emit(queue, {"step": "error", "status": "error", "message": f"规划恢复失败: {exc}", "progress": -1})
    finally:
        _task_running.discard(task_id)


def resume_planning_background(task_id: str, payload: dict[str, Any]) -> None:
    """在 FastAPI 后台 resume 规划 interrupt。"""
    async def _wrap() -> None:
        try:
            from swarm.logging_config import bind_task
            with bind_task(task_id):
                await resume_planning(task_id, payload)
        except Exception:  # noqa: BLE001
            logger.exception("[RUNNER] resume_planning_background 失败 task=%s", task_id)
    _task_handles[task_id] = asyncio.create_task(_wrap())


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

    status = task.get("status", "")

    # 人工审核态优先拦截：即使本进程未在跑（orphaned），这类任务也需要
    # 先由人工 通过/修订/拒绝 决策，而不是直接重跑（否则丢失待审产出）。
    if status in ("DELIVERING", "CONFIRMING"):
        return False, "任务等待人工审核，请先通过/修订/拒绝"

    if is_task_orphaned(task_id):
        return True, ""

    if status in ("FAILED", "CANCELLED", "DONE"):
        return True, ""

    if status in _ACTIVE_DB_STATUSES:
        return False, "任务仍在执行中"

    return False, f"当前状态 {status} 不可重跑"


async def cancel_task(task_id: str) -> bool:
    """取消正在运行的任务，或将 orphaned 活跃任务标记为 CANCELLED。

    即使 DB 记录已不存在（如项目被删），仍必须取消内存中的 asyncio 句柄 +
    释放沙箱——否则 asyncio 任务会变成幽灵，陷入 replan 死循环持续烧 GPU。
    """
    task = store.get_task(task_id)

    handle = _task_handles.get(task_id)
    handle_cancelled = False
    if handle and not handle.done():
        handle.cancel()
        handle_cancelled = True
        try:
            await handle
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — 句柄内部异常不应阻断清理
            pass

    _task_running.discard(task_id)

    # 释放该任务占用的沙箱（释放远程小模型/容器资源）——取消时容器不会自动销毁。
    # CancelledError 不保证传播到 worker 的 finally（取消时机可能不在 await 点，
    # 或 brain 级 L2/L3 sandbox 不在 worker 生命周期内），故在此显式按 task 清理。
    try:
        from swarm.worker.sandbox import get_sandbox_manager

        killed = get_sandbox_manager().kill_by_task(task_id)
        if killed:
            logger.info("[RUNNER] 取消任务 %s 释放 %d 个沙箱", task_id, killed)
    except Exception as exc:
        logger.warning("[RUNNER] 取消任务 %s 释放沙箱失败: %s", task_id, exc)

    queue = _task_queues.get(task_id)
    if queue:
        await _emit(queue, {
            "step": "cancelled",
            "status": "cancelled",
            "message": "任务已取消",
            "progress": -1,
        })

    # DB 记录已被删（如级联删项目）→ 仅完成了内存侧清理，仍算成功取消。
    if task is None:
        if handle_cancelled:
            logger.info("[RUNNER] 任务 %s 无 DB 记录(可能项目已删)，已终止内存句柄+沙箱", task_id)
        return handle_cancelled

    if task.get("status") != "CANCELLED":
        store.update_task(task_id, status="CANCELLED")
    return True


async def cancel_project_tasks(project_id: str) -> int:
    """取消某项目下所有运行中的任务（删项目前调用，防止幽灵任务残留）。

    覆盖两类：(1) 内存中有 asyncio 句柄的活跃任务；(2) DB 标记为活跃状态的任务。
    返回取消的任务数。
    """
    cancelled = 0
    # 1) 内存中所有句柄属于该项目的（句柄字典只有 task_id，需查 DB 反查 project）
    candidate_ids: set[str] = set()
    for tid, handle in list(_task_handles.items()):
        if handle and not handle.done():
            candidate_ids.add(tid)
    # 2) DB 中该项目活跃状态的任务
    try:
        for t in store.list_tasks(project_id):
            if t.get("status") in _ACTIVE_DB_STATUSES:
                candidate_ids.add(t.get("id"))
    except Exception as exc:
        logger.warning("[RUNNER] 枚举项目 %s 活跃任务失败: %s", project_id, exc)

    # 对候选逐个取消（cancel_task 已能处理 DB 记录缺失的情况）
    for tid in candidate_ids:
        # 仅取消属于该项目的：若 DB 还能查到则校验 project_id，查不到则按内存句柄取消
        t = store.get_task(tid)
        if t is not None and t.get("project_id") != project_id:
            continue
        try:
            if await cancel_task(tid):
                cancelled += 1
        except Exception as exc:
            logger.warning("[RUNNER] 级联取消任务 %s 失败: %s", tid, exc)
    if cancelled:
        logger.info("[RUNNER] 项目 %s 级联取消 %d 个运行中任务", project_id, cancelled)
    return cancelled


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
        from swarm.logging_config import bind_task

        with bind_task(task_id):
            try:
                await run_task(task_id, project_id, description, auto_accept=auto_accept)
            finally:
                _task_handles.pop(task_id, None)

    _task_handles[task_id] = asyncio.create_task(_wrap())


def resume_task_background(task_id: str, decision: str, feedback: str = "") -> None:
    """在 FastAPI 后台 resume 任务"""
    async def _wrap() -> None:
        from swarm.logging_config import bind_task

        with bind_task(task_id):
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
        from swarm.logging_config import bind_task

        with bind_task(task_id):
            try:
                await retry_task(task_id, auto_accept=auto_accept)
            finally:
                _task_handles.pop(task_id, None)

    _task_handles[task_id] = asyncio.create_task(_wrap())
