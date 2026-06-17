"""api/routers/worker.py — Worker 域路由(直跑/SSE流/应用diff)

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(store/_validate_project/_get_pg_conn 等)用 _app. 属性访问保测试零改动。
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import swarm.api.app as _app
from swarm.api._shared import ApplyDiffRequest, _require_perm

router = APIRouter()


class WorkerRunRequest(BaseModel):
    """Phase 0 — 单 Worker 直跑（不经 Brain）"""
    description: str = Field(description="子任务描述")
    difficulty: str = Field(default="medium", description="trivial | medium | complex")
    writable: list[str] | None = Field(default=None, description="可写路径，默认全项目")
    readable: list[str] | None = Field(default=None, description="可读路径，默认全项目")




# ─── Phase 0: POST /api/projects/{project_id}/worker/run ───
@router.post("/api/projects/{project_id}/worker/run", tags=["Worker"])
async def start_worker_run(project_id: str, req: WorkerRunRequest, request: Request):
    """单 Worker 直跑（不经 Brain），用于 Phase 0 验证 scope + L1 + diff"""
    _require_perm(request, "worker:run", project_id)  # P0-SEC-02：起 worker（owner/developer 均有 worker:run）
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    run_id = str(uuid.uuid4())
    from swarm.worker.runner import start_standalone_worker_background

    start_standalone_worker_background(
        run_id,
        project_id,
        req.description,
        difficulty=req.difficulty,
        writable=req.writable,
        readable=req.readable,
    )
    return {"status": "ok", "run_id": run_id, "project_id": project_id}


# ─── Phase 0: GET /api/worker/{run_id}/stream ───
@router.get("/api/worker/{run_id}/stream", tags=["Worker"])
async def stream_worker_run(run_id: str, request: Request):
    """SSE 订阅 Standalone Worker 进度"""
    from swarm.api._shared import _require_user
    _require_user(request)  # P0-SEC-03：worker 进度流至少需认证（run_id 非项目映射）
    from swarm.worker.runner import get_worker_queue, register_worker_queue

    queue = get_worker_queue(run_id) or register_worker_queue(run_id)

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

    from swarm.project.diff_apply import apply_git_diff

    result = await loop.run_in_executor(
        None,
        lambda: apply_git_diff(project["path"], req.diff or "", check_only=req.check_only),
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=422,
            detail=result.get("stderr") or result.get("stdout") or "git apply 失败",
        )
    return {"status": "ok", **result}
