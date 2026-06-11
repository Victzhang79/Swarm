"""api/routers/task.py — 任务管理域路由 (列表/创建/SSE流/详情/删除/取消/重试/审批/diff/修订/拒绝)。

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(store)及 app 级 get_config/logger 用 _app. 属性访问。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import swarm.api.app as _app
from swarm.api._shared import ApplyDiffRequest, _require_perm

router = APIRouter()


class TaskCreateRequest(BaseModel):
    """创建任务请求"""
    description: str = Field(description="任务描述")
    auto_accept: bool = Field(default=False, description="自动通过审核（E2E/演示）")
    priority: str = Field(default="normal", description="队列优先级: urgent / normal / background")
    force: bool = Field(default=False, description="跳过重复检测，强制新建（即使有同描述的进行中任务）")


class TaskReviseRequest(BaseModel):
    """审核修订请求"""
    feedback: str = Field(description="修订反馈意见")


class TaskRetryRequest(BaseModel):
    """重跑任务请求"""
    auto_accept: bool | None = Field(default=None, description="自动通过审核（默认沿用环境变量）")


class ApproveTaskRequest(BaseModel):
    """审核通过选项"""
    apply_diff: bool = Field(
        default=False,
        description="显式 git apply；sandbox_first 模式下通常已由 pull-back 写回本地",
    )


@router.get("/api/projects/{project_id}/tasks", tags=["任务管理"])
async def list_tasks(project_id: str):
    """获取项目下的所有任务"""
    loop = asyncio.get_running_loop()
    # 确认项目存在
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    tasks = await loop.run_in_executor(None, _app.store.list_tasks, project_id)
    return {"tasks": tasks}


# ─── 8. POST /api/projects/{project_id}/tasks — 创建任务 ─
@router.post("/api/projects/{project_id}/tasks", tags=["任务管理"])
async def create_task(project_id: str, req: TaskCreateRequest, request: Request):
    """创建任务并后台启动 Brain 编排"""
    user = _require_perm(request, "task:create", project_id)
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    from swarm.knowledge.readiness import brain_task_ready

    progress = await loop.run_in_executor(None, _app.store.get_progress, project_id)
    ready, reason = brain_task_ready(project, progress)
    if not ready:
        raise HTTPException(
            status_code=409,
            detail=reason or "项目知识库未就绪，请先完成预处理",
        )

    # 去重：同项目内已有「相同描述 + 进行中（非终态）」任务时，默认不重复创建，
    # 直接复用并返回已有任务（避免误触/重复提交建出多个一样的任务）。
    # force=true 可显式绕过。终态任务不算重复，允许重新发起。
    if not req.force:
        dup = await loop.run_in_executor(
            None,
            lambda: _app.store.find_active_duplicate_task(project_id, req.description),
        )
        if dup:
            return {
                "status": "duplicate",
                "task": dup,
                "message": (
                    f"已存在进行中的同描述任务（#{str(dup.get('id'))[:8]}，"
                    f"状态 {dup.get('status')}）。如需强制新建请用 force=true。"
                ),
            }

    task_id = str(uuid.uuid4())
    try:
        task = await loop.run_in_executor(
            None,
            lambda: _app.store.create_task(
                task_id=task_id,
                project_id=project_id,
                description=req.description,
                created_by_user_id=user.id,
            ),
        )
        await loop.run_in_executor(
            None,
            lambda: _app.store.update_task(task_id, status="SUBMITTED", thread_id=task_id),
        )
    except Exception as e:
        _app.logger.error(f"Failed to create task: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create task: {str(e)}")

    from swarm.brain.scheduler import submit_task

    # 入优先级队列，由准入调度器按并发上限执行（urgent>normal>background）
    priority = getattr(req, "priority", "normal") or "normal"
    submit_task(
        task_id, project_id, req.description,
        auto_accept=req.auto_accept, priority=priority,
    )

    # 应用内通知：任务已建立（带 task_id）
    short = (req.description or "")[:80]
    await loop.run_in_executor(
        None,
        lambda: _app.store.create_notification(
            "task_created",
            task_id=task_id,
            project_id=project_id,
            title="任务已建立",
            message=f"#{task_id[:8]} {short}",
        ),
    )

    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    return {"status": "ok", "task": task}




# ─── 9. GET /api/tasks/{task_id}/stream — SSE 任务进度 ─
@router.get("/api/tasks/{task_id}/stream", tags=["任务管理"])
async def stream_task(task_id: str):
    """SSE 流式推送任务 Brain 执行进度"""
    from swarm.brain.runner import get_task_queue, register_task_queue

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    queue = get_task_queue(task_id) or register_task_queue(task_id)

    async def event_generator():
        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": ""}
                    continue

                step = event_data.get("step", "")
                event_type = "progress"
                if step == "result":
                    event_type = "result"
                elif step == "error":
                    event_type = "error"
                elif step == "awaiting_review":
                    event_type = "awaiting_review"

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data, ensure_ascii=False, default=str),
                }

                if step in ("complete", "error", "awaiting_review"):
                    break
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


# ─── 9b. WS /ws/tasks/{task_id} — WebSocket 任务进度（与 SSE 并存）──
@router.websocket("/ws/tasks/{task_id}")
async def ws_task_progress(websocket: WebSocket, task_id: str):
    """WebSocket 推送任务 Brain 执行进度

    复用 SSE 的同一个 asyncio.Queue 事件源，通过 WebSocket 传输。
    消息格式: JSON {"event": "progress"|"result"|"error"|"heartbeat", "data": {...}}
    连接断开时优雅处理。
    """
    from swarm.brain.runner import get_task_queue, register_task_queue

    await websocket.accept()

    # 校验任务是否存在
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        await websocket.send_json({"event": "error", "data": {"detail": f"Task {task_id} not found"}})
        await websocket.close()
        return

    queue = get_task_queue(task_id) or register_task_queue(task_id)

    try:
        while True:
            try:
                event_data = await asyncio.wait_for(queue.get(), timeout=30)
            except asyncio.TimeoutError:
                # 心跳：防止连接空闲超时
                await websocket.send_json({"event": "heartbeat", "data": ""})
                continue

            step = event_data.get("step", "")
            event_type = "progress"
            if step == "result":
                event_type = "result"
            elif step == "error":
                event_type = "error"
            elif step == "awaiting_review":
                event_type = "awaiting_review"

            await websocket.send_json({
                "event": event_type,
                "data": event_data,
            })

            # 终止事件
            if step in ("complete", "error", "awaiting_review"):
                break
    except WebSocketDisconnect:
        # 客户端断开连接 — 优雅退出
        pass
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _app.logger.warning("WebSocket /ws/tasks/%s 异常: %s", task_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ─── 9. GET /api/tasks/{task_id} — 任务详情 ──────
# 注：/api/tasks/audit 必须在此【之前】注册，否则 'audit' 会被 {task_id} 捕获。
@router.get("/api/tasks/audit", tags=["任务管理"])
async def task_audit_endpoint(task_id: str = "", project_id: str = "", limit: int = 100):
    """查询任务审计日志（append-only，含已删除任务的生命周期留痕）。

    解决可追溯性：即使任务/项目被硬删，仍能在此查到它的创建/删除记录与描述。
    """
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        None,
        lambda: _app.store.list_task_audit(
            task_id=task_id or None, project_id=project_id or None, limit=limit
        ),
    )
    return {"status": "ok", "audit": rows}


@router.get("/api/tasks/{task_id}", tags=["任务管理"])
async def get_task(task_id: str):
    """获取任务详情"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"task": jsonable_encoder(task)}


@router.delete("/api/tasks/{task_id}", tags=["任务管理"])
async def delete_task_endpoint(task_id: str, force: bool = False):
    """删除任务；force=true 时先取消运行中任务；orphaned 活跃任务可直接删除"""
    from swarm.brain.runner import (
        _ACTIVE_DB_STATUSES,
        cancel_task,
        is_task_orphaned,
        is_task_running,
    )

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    status = task.get("status", "")
    if is_task_running(task_id):
        if not force:
            raise HTTPException(status_code=409, detail="任务正在执行中，请使用 force=true 强制删除")
        await cancel_task(task_id)
    elif status in _ACTIVE_DB_STATUSES and not is_task_orphaned(task_id):
        if not force:
            raise HTTPException(status_code=409, detail="任务处于活跃状态，请使用 force=true 强制删除")

    deleted = await loop.run_in_executor(None, _app.store.delete_task, task_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="删除失败")
    return {"status": "ok", "message": f"任务 {task_id} 已删除"}


@router.post("/api/tasks/{task_id}/cancel", tags=["任务管理"])
async def cancel_task_endpoint(task_id: str):
    """取消运行中任务，或将 orphaned 活跃任务标记为已取消"""
    from swarm.brain.runner import cancel_task, is_task_orphaned, is_task_running

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    if not is_task_running(task_id) and not is_task_orphaned(task_id):
        status = task.get("status", "")
        if status in ("CANCELLED", "FAILED", "DONE"):
            return {"status": "ok", "task": task, "message": "任务已结束，无需取消"}
        raise HTTPException(status_code=409, detail=f"任务状态 {status} 不可取消")

    await cancel_task(task_id)
    updated = await loop.run_in_executor(None, _app.store.get_task, task_id)
    return {"status": "ok", "task": jsonable_encoder(updated), "message": "任务已取消"}


@router.post("/api/tasks/{task_id}/retry", tags=["任务管理"])
async def retry_task_endpoint(task_id: str, req: TaskRetryRequest | None = None):
    """重跑失败/已取消/orphaned 任务"""
    from swarm.brain.runner import can_retry_task, register_task_queue, retry_task_background

    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    allowed, reason = can_retry_task(task_id)
    if not allowed:
        raise HTTPException(status_code=409, detail=reason or "当前状态不可重跑")

    auto_accept = req.auto_accept if req else None
    register_task_queue(task_id)
    retry_task_background(task_id, auto_accept=auto_accept)
    return {"status": "ok", "task": jsonable_encoder(task), "message": "已提交重跑，Brain 重新执行"}


# ─── GET /api/tasks/{task_id}/logs — 该任务执行日志 ─
@router.get("/api/tasks/{task_id}/logs", tags=["任务管理"])
async def get_task_logs(task_id: str, limit: int = 500):
    """读取某任务的执行日志（从 swarm.log 按 [task=前8位] 过滤）。

    依赖统一日志系统的 task 上下文前缀（swarm.logging_config.bind/set_task_context）。
    """
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    from swarm.logging_config import read_task_logs, resolve_log_path

    limit = max(1, min(int(limit or 500), 2000))
    lines = await loop.run_in_executor(None, lambda: read_task_logs(task_id, limit=limit))
    log_path = resolve_log_path()
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "count": len(lines),
        "lines": lines,
        "log_file": str(log_path) if log_path else None,
        "hint": "" if lines else "暂无该任务日志（可能任务在日志轮转前执行，或日志文件未配置）",
    }


# ─── GET /api/tasks/{task_id}/logs/stream — 实时日志 SSE ─
@router.get("/api/tasks/{task_id}/logs/stream", tags=["任务管理"])
async def stream_task_logs(task_id: str):
    """SSE 实时推送某任务的执行日志（tail swarm.log 按 [task=前8位] 过滤）。

    纯文件读，不触发任何任务执行。任务进入终态后自动结束流。
    认证：中间件从 ?token= 读取（EventSource 不能带 Authorization 头）。
    """
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    from swarm.logging_config import TaskLogPoller

    _TERMINAL = {"DONE", "FAILED", "CANCELLED"}

    async def event_generator():
        poller = TaskLogPoller(task_id)
        terminal_idle = 0
        try:
            while True:
                batch = await loop.run_in_executor(None, poller.poll)
                if batch:
                    for line in batch:
                        yield {"event": "log", "data": line}
                    terminal_idle = 0
                    continue

                # 无新行：心跳，并检查任务是否已终态
                yield {"event": "heartbeat", "data": ""}
                cur = await loop.run_in_executor(None, _app.store.get_task, task_id)
                if cur and cur.get("status") in _TERMINAL:
                    terminal_idle += 1
                    # 终态后再多轮询一次确保尾部日志吐完，然后收尾
                    if terminal_idle >= 2:
                        yield {"event": "end", "data": cur.get("status")}
                        break
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


# ─── 10. POST /api/tasks/{task_id}/approve — 审核通过 ─
@router.post("/api/tasks/{task_id}/approve", tags=["任务管理"])
async def approve_task(task_id: str, req: ApproveTaskRequest | None = None):
    """审核通过 — 可选 apply diff + 增量知识更新，然后 resume Brain"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    project = await loop.run_in_executor(None, _app.store.get_project, task["project_id"])
    merged_diff = task.get("merged_diff") or ""
    apply_diff_flag = req.apply_diff if req else False
    cfg = _app.get_config()
    should_apply = apply_diff_flag or (
        not cfg.sandbox.sandbox_first and bool(merged_diff.strip())
    )
    apply_result: dict[str, Any] | None = None

    if should_apply and merged_diff.strip() and project and project.get("path"):
        from swarm.project.diff_apply import apply_git_diff

        apply_result = await loop.run_in_executor(
            None,
            lambda: apply_git_diff(project["path"], merged_diff, check_only=False),
        )
        if apply_diff_flag and apply_result and not apply_result.get("ok"):
            raise HTTPException(
                status_code=422,
                detail=apply_result.get("stderr") or apply_result.get("stdout") or "git apply 失败",
            )

    if merged_diff.strip() and project and project.get("path"):
        from swarm.knowledge.hooks import schedule_incremental_update

        schedule_incremental_update(
            task["project_id"],
            project["path"],
            merged_diff,
            task_id=task_id,
        )

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "accept")
    updated = await loop.run_in_executor(
        None,
        lambda: _app.store.update_task(task_id, human_decision="ACCEPT"),
    )
    out: dict[str, Any] = {"status": "ok", "task": updated, "message": "已提交接受，Brain 继续执行"}
    if apply_result:
        out["apply_diff"] = apply_result

    # NOTE(tech-debt): 此处发出的是"审批事件"通知（task_approved），语义正确。
    # 待办：未来应在 Brain runner 任务真正执行完成时，额外发一条"完成事件"通知
    # （需在 brain/runner.py 任务生命周期回调挂钩）。当前两类事件未区分，属已知缺口。
    from swarm.api.notify import notify
    await notify("task_approved", task_id, f"任务 {task_id} 已审核通过，Brain 继续执行")

    return out


@router.post("/api/tasks/{task_id}/apply-diff", tags=["任务管理"])
async def apply_task_diff(task_id: str, req: ApplyDiffRequest | None = None):
    """Phase 1 — 将 merged_diff 应用到项目 git 工作区（git apply）"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    project = await loop.run_in_executor(None, _app.store.get_project, task["project_id"])
    if not project or not project.get("path"):
        raise HTTPException(status_code=400, detail="项目路径不可用")

    diff = (req.diff if req and req.diff else None) or task.get("merged_diff") or ""
    if not diff.strip():
        raise HTTPException(status_code=400, detail="任务无 merged_diff 可应用")

    conflicts = task.get("merge_conflicts") or []
    if conflicts and not (req and req.check_only):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "存在 merge 冲突，请先解决冲突后再 apply",
                "merge_conflicts": conflicts,
            },
        )
    if conflicts and req and req.check_only:
        return {
            "status": "conflict",
            "ok": False,
            "message": "merge 冲突 — git apply 已阻断",
            "merge_conflicts": conflicts,
        }

    check_only = req.check_only if req else False
    from swarm.project.diff_apply import apply_git_diff

    result = await loop.run_in_executor(
        None,
        lambda: apply_git_diff(project["path"], diff, check_only=check_only),
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=422,
            detail=result.get("stderr") or result.get("stdout") or "git apply 失败",
        )
    return {"status": "ok", **result}


# ─── 11. POST /api/tasks/{task_id}/revise — 审核修订 ─
@router.post("/api/tasks/{task_id}/revise", tags=["任务管理"])
async def revise_task(task_id: str, req: TaskReviseRequest):
    """审核修订 — resume Brain (revise + feedback)"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "revise", req.feedback)
    updated = await loop.run_in_executor(
        None,
        lambda: _app.store.update_task(task_id, human_decision="REVISE"),
    )
    # NOTE(tech-debt): 发出的是"审批事件"通知（task_revised），语义正确。
    # 待办：完成事件通知应在 brain/runner.py 任务生命周期回调中补充（见 accept 同款注释）。
    from swarm.api.notify import notify
    await notify("task_revised", task_id, f"任务 {task_id} 已提交修订，Brain 重新调度")
    return {"status": "ok", "task": updated, "message": "已提交修订，Brain 重新调度"}


# ─── 12. POST /api/tasks/{task_id}/reject — 审核拒绝 ─
@router.post("/api/tasks/{task_id}/reject", tags=["任务管理"])
async def reject_task(task_id: str):
    """审核拒绝 — resume Brain (reject)"""
    loop = asyncio.get_running_loop()
    task = await loop.run_in_executor(None, _app.store.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    from swarm.brain.runner import register_task_queue, resume_task_background

    register_task_queue(task_id)
    resume_task_background(task_id, "reject")
    updated = await loop.run_in_executor(
        None,
        lambda: _app.store.update_task(task_id, human_decision="REJECT"),
    )
    # NOTE(tech-debt): 发出的是"审批事件"通知（task_rejected），语义正确。
    # 待办：完成事件通知应在 brain/runner.py 任务生命周期回调中补充（见 accept 同款注释）。
    from swarm.api.notify import notify
    await notify("task_rejected", task_id, f"任务 {task_id} 已拒绝，Brain 进入学习失败流程")
    return {"status": "ok", "task": updated, "message": "已拒绝，Brain 进入学习失败流程"}
