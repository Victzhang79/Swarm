"""api/routers/worker.py — Worker 域路由(直跑/SSE流/应用diff)

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(store/_validate_project/_get_pg_conn 等)用 _app. 属性访问保测试零改动。
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from swarm.api.rate_limit import rate_limit  # C7
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import swarm.api.app as _app
from swarm.api._shared import ApplyDiffRequest, _require_perm

router = APIRouter()


class WorkerRunRequest(BaseModel):
    """Phase 0 — 单 Worker 直跑（不经 Brain）"""
    description: str = Field(description="子任务描述")
    difficulty: str = Field(default="medium", description="trivial | medium | complex")
    writable: list[str] | None = Field(
        default=None,
        description="可写路径；两类路径都留空时才是全项目，否则仅限显式清单",
    )
    readable: list[str] | None = Field(
        default=None,
        description="额外可读路径；部分 scope 模式仅限显式清单与可写路径",
    )
    create_files: list[str] | None = Field(
        default=None,
        description="允许新建的路径；声明后启用最小权限模式",
    )
    delete_files: list[str] | None = Field(
        default=None,
        description="允许删除的精确路径；声明后启用最小权限模式",
    )


class WorkspaceQuarantineClearRequest(BaseModel):
    expected_token: str = Field(min_length=1, max_length=128)


@router.get("/api/projects/{project_id}/worker/quarantine", tags=["Worker"])
async def inspect_worker_quarantine(project_id: str, request: Request):
    """查看共享工作树隔离态；路径和错误细节仅对项目写者开放。"""
    _require_perm(request, "project:write", project_id)
    project = await asyncio.to_thread(_app.store.get_project, project_id)
    if not project or not project.get("path"):
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    from swarm.worker.workspace_quarantine import load_workspace_quarantine

    record = await load_workspace_quarantine(project_id, project["path"])
    if record is None:
        return {"status": "ok", "active": False}
    public = {key: value for key, value in record.items() if not key.startswith("_")}
    return {"status": "ok", **public}


@router.post(
    "/api/projects/{project_id}/worker/quarantine/clear",
    tags=["Worker"],
    dependencies=[Depends(rate_limit("worker_quarantine_clear", capacity=5, rate=0.1))],
)
async def clear_worker_quarantine(
    project_id: str,
    req: WorkspaceQuarantineClearRequest,
    request: Request,
):
    """人工修复工作树后，持项目写锁并以 token CAS 解封。"""
    _require_perm(request, "project:write", project_id)
    project = await asyncio.to_thread(_app.store.get_project, project_id)
    if not project or not project.get("path"):
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    if project.get("status") == "DELETING":
        raise HTTPException(status_code=409, detail="项目正在删除，拒绝修改隔离态")
    from swarm.worker.workspace_quarantine import (
        clear_workspace_quarantine,
        load_workspace_quarantine,
    )

    try:
        cleared = await clear_workspace_quarantine(
            project_id, project["path"], req.expected_token
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"隔离态清除未确认: {exc}") from exc
    if not cleared:
        raise HTTPException(
            status_code=409,
            detail="项目写锁忙、隔离态不存在或 token 已更新，请重新查询后重试",
        )
    remaining = await load_workspace_quarantine(project_id, project["path"])
    if remaining is not None:
        public = {
            key: value for key, value in remaining.items() if not key.startswith("_")
        }
        return {"status": "ok", **public}
    return {"status": "ok", "active": False, "pending_incidents": 0}




# ─── Phase 0: POST /api/projects/{project_id}/worker/run ───
@router.post("/api/projects/{project_id}/worker/run", tags=["Worker"],
             dependencies=[Depends(rate_limit("worker_run", capacity=10, rate=0.2))])  # C7
async def start_worker_run(project_id: str, req: WorkerRunRequest, request: Request):
    """单 Worker 直跑（不经 Brain），用于 Phase 0 验证 scope + L1 + diff"""
    _require_perm(request, "worker:run", project_id)  # P0-SEC-02：起 worker（owner/developer 均有 worker:run）
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    if project.get("status") == "DELETING":
        raise HTTPException(status_code=409, detail="项目正在删除，不能启动 Worker")

    run_id = str(uuid.uuid4())
    from swarm.worker.runner import start_standalone_worker_background

    start_standalone_worker_background(
        run_id,
        project_id,
        req.description,
        difficulty=req.difficulty,
        writable=req.writable,
        readable=req.readable,
        create_files=req.create_files,
        delete_files=req.delete_files,
    )
    return {"status": "ok", "run_id": run_id, "project_id": project_id}


# ─── Phase 0: GET /api/worker/{run_id}/stream ───
@router.get("/api/worker/{run_id}/stream", tags=["Worker"])
async def stream_worker_run(run_id: str, request: Request):
    """SSE 订阅 Standalone Worker 进度"""
    from swarm.worker.runner import (
        get_worker_queue,
        get_worker_run_project,
        register_worker_queue,
    )

    # A-P1-28：run 进度流必须按【该 run 归属项目】做所有权/成员校验（task:read），
    # 而非只验认证——否则任一已认证用户拿到 run_id 即可订阅他人 worker 流（越权读）。
    # 未知 run_id（无映射）一律 404，fail-closed：既不泄露存在性，也不为任意 run_id 建队。
    project_id = get_worker_run_project(run_id)
    if project_id is None:
        raise HTTPException(status_code=404, detail=f"Worker run {run_id} not found")
    _require_perm(request, "task:read", project_id)

    queue = get_worker_queue(run_id) or register_worker_queue(run_id)

    async def event_generator():
        reauth_interval = 30.0
        next_reauth_at = asyncio.get_running_loop().time() + reauth_interval
        try:
            while True:
                timed_out = False
                try:
                    remaining = max(0.001, next_reauth_at - asyncio.get_running_loop().time())
                    event_data = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    event_data = None
                if asyncio.get_running_loop().time() >= next_reauth_at:
                    from swarm.api.routers.task import _stream_reauthorized
                    if not await asyncio.to_thread(
                        _stream_reauthorized,
                        request,
                        {"project_id": project_id},
                        "task:read",
                    ):
                        yield {"event": "end", "data": "auth_revoked"}
                        break
                    next_reauth_at = asyncio.get_running_loop().time() + reauth_interval
                if timed_out:
                    yield {"event": "heartbeat", "data": ""}
                    continue

                step = event_data.get("step", "")
                event_type = "progress"
                if step == "result":
                    event_type = "result"
                elif step == "error":
                    event_type = "error"

                yield {
                    "event": event_type,
                    "data": json.dumps(event_data, ensure_ascii=False, default=str),
                }
                if step in ("complete", "error"):
                    break
        except asyncio.CancelledError:
            pass

    return EventSourceResponse(event_generator())


@router.post("/api/projects/{project_id}/apply-diff", tags=["Worker"])
async def apply_project_diff(project_id: str, req: ApplyDiffRequest, request: Request):
    """Phase 0/1 — 将 diff 应用到项目 git 工作区（Worker 直跑或手动 patch）"""
    _require_perm(request, "task:approve", project_id)  # P0-SEC-02：应用 diff=接受产出（owner/developer 可）
    if not req or not (req.diff or "").strip():
        raise HTTPException(status_code=400, detail="请求体须包含 diff 字段")
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project or not project.get("path"):
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    from swarm.infra.redis_client import ModuleLock
    from swarm.infra.cancellation import run_blocking_owned
    from swarm.project.diff_apply import apply_git_diff

    # 5.9 复核 #10（HIGH）：锁外直写树 sibling——真写须持 runner 同把模块锁（同 E9）。
    _lk = None
    if not req.check_only:
        _lk = ModuleLock(project_id, "default")
        if not await run_blocking_owned(
            _lk.acquire,
            operation=f"Worker 手动 diff 获取项目锁 project={project_id}",
            cancel_result_cleanup=lambda result: _lk.release() if result else None,
        ):
            raise HTTPException(
                status_code=409,
                detail="同项目有任务正在写工作树（模块锁被占用），请稍后重试",
            )
    try:
        if _lk is not None:
            locked_project = await loop.run_in_executor(
                None, _app.store.get_project, project_id
            )
            if (
                not locked_project
                or locked_project.get("status") == "DELETING"
                or not locked_project.get("path")
            ):
                raise HTTPException(
                    status_code=409,
                    detail="项目已删除或正在删除，拒绝应用 diff",
                )
            project = locked_project
        result = await run_blocking_owned(
            lambda: apply_git_diff(project["path"], req.diff or "", check_only=req.check_only),
            operation=f"Worker 手动应用 diff project={project_id}",
        )
    finally:
        if _lk is not None:
            await run_blocking_owned(
                _lk.release,
                operation=f"Worker 手动 diff 释放项目锁 project={project_id}",
            )
    if not result.get("ok"):
        raise HTTPException(
            status_code=422,
            detail=result.get("stderr") or result.get("stdout") or "git apply 失败",
        )
    return {"status": "ok", **result}
