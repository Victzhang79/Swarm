"""api/routers/project.py — 项目管理域路由 (列表/创建/详情/删除/预处理触发与进度)。

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(store/_validate_project)及 app 级 preprocess/logger 用 _app. 属性访问。
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import swarm.api.app as _app
from swarm.api._shared import _require_perm, _require_user

router = APIRouter()


class ProjectCreateRequest(BaseModel):
    """创建项目请求"""
    name: str = Field(description="项目名称")
    path: str = Field(default="", description="项目根目录绝对路径；greenfield 留空则自动在 workspace 下创建")
    description: str = Field(default="", description="项目描述")
    greenfield: bool = Field(default=False, description="从零创建（空项目），path 不存在时自动建目录")


@router.get("/api/projects", tags=["项目管理"])
async def list_projects(request: Request):
    """返回当前用户可见的项目列表"""
    from swarm.auth.rbac import Role
    from swarm.auth.store import list_user_project_ids

    user = _require_user(request)
    loop = asyncio.get_running_loop()
    try:
        all_projects = await loop.run_in_executor(None, _app.store.list_projects)
    except Exception as e:
        _app.logger.warning(f"PG unavailable for list_projects: {e}")
        all_projects = []
    if user.global_role != Role.ADMIN.value:
        allowed = list_user_project_ids(user.id)
        all_projects = [p for p in all_projects if p.get("id") in allowed]
    return {"projects": all_projects}


# ─── 2. POST /api/projects — 创建项目 ─────────────
@router.post("/api/projects", tags=["项目管理"])
async def create_project(req: ProjectCreateRequest, request: Request):
    """创建项目并自动启动预处理

    项目状态从 EMPTY → PREPROCESSING → READY
    """
    from swarm.auth.rbac import Role
    from swarm.auth.store import set_project_member

    user = _require_perm(request, "project:create")
    project_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()

    # ── 路径解析 + greenfield（从零创建）支持 ──
    # 既有项目：path 必须指向存在的目录。
    # greenfield：path 不存在则自动创建；留空则在 workspace 下按项目名建目录。
    import os
    import re as _re
    from swarm.config.settings import PROJECT_ROOT

    resolved_path = (req.path or "").strip()
    if req.greenfield:
        if not resolved_path:
            safe = _re.sub(r"[^A-Za-z0-9_.-]+", "-", req.name).strip("-") or project_id[:8]
            resolved_path = str((PROJECT_ROOT / "workdir" / safe).resolve())
        try:
            os.makedirs(resolved_path, exist_ok=True)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"无法创建项目目录 {resolved_path}: {e}") from e
    else:
        if not resolved_path:
            raise HTTPException(status_code=400, detail="既有项目必须提供 path（或设 greenfield=true 从零创建）")
        if not os.path.isdir(resolved_path):
            raise HTTPException(
                status_code=400,
                detail=f"路径不存在: {resolved_path}。如需从零创建空项目，请设 greenfield=true",
            )

    # 创建项目记录
    try:
        project = await loop.run_in_executor(
            None,
            lambda: _app.store.create_project(
                project_id=project_id,
                name=req.name,
                path=resolved_path,
                description=req.description,
            ),
        )
        if user.global_role != Role.ADMIN.value:
            await loop.run_in_executor(
                None,
                lambda: set_project_member(project_id, user.id, Role.OWNER.value),
            )
    except Exception as e:
        _app.logger.error(f"Failed to create project: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create project: {str(e)}")

    # 后台启动预处理（不阻塞响应）
    async def _run_preprocess():
        try:
            from swarm.project.preprocess import preprocess_project
            await preprocess_project(project_id, resolved_path)
        except Exception as e:
            _app.logger.error(f"Preprocessing failed for project {project_id}: {e}")

    asyncio.create_task(_run_preprocess())

    return {"status": "ok", "project": project}


# ─── 3. GET /api/projects/{project_id} — 项目详情 ─
@router.get("/api/projects/{project_id}", tags=["项目管理"])
async def get_project(project_id: str, request: Request):
    """获取项目详情"""
    _require_perm(request, "project:read", project_id)
    loop = asyncio.get_running_loop()
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return {"project": project}


# ─── 4. DELETE /api/projects/{project_id} — 删除项目 ─
@router.delete("/api/projects/{project_id}", tags=["项目管理"])
async def delete_project(project_id: str, request: Request):
    """删除项目及其关联数据"""
    _require_perm(request, "project:delete", project_id)
    loop = asyncio.get_running_loop()
    # 先确认项目存在
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    deleted = await loop.run_in_executor(None, _app.store.delete_project, project_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete project")
    return {"status": "ok", "message": f"Project {project_id} deleted"}


# ─── 5. POST /api/projects/{project_id}/preprocess — 手动触发预处理 ─
@router.post("/api/projects/{project_id}/preprocess", tags=["项目管理"])
async def trigger_preprocess(project_id: str):
    """手动触发/重新触发项目预处理"""
    loop = asyncio.get_running_loop()
    try:
        project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    except Exception as e:
        _app.logger.exception("Failed to load project %s for preprocess", project_id)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {e}") from e

    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    project_path = project["path"]

    try:
        await loop.run_in_executor(None, _app.store.reset_preprocess_progress, project_id)
        await loop.run_in_executor(
            None,
            lambda: _app.store.update_project(project_id, status="PREPROCESSING"),
        )
    except Exception as e:
        _app.logger.exception("Failed to reset preprocess state for %s", project_id)
        raise HTTPException(status_code=500, detail=f"Failed to start preprocess: {e}") from e

    # 后台启动预处理
    async def _run_preprocess():
        try:
            from swarm.project.preprocess import preprocess_project
            await preprocess_project(project_id, project_path)
        except Exception:
            _app.logger.exception("Preprocessing failed for project %s", project_id)

    asyncio.create_task(_run_preprocess())
    _app.logger.info("Preprocess queued for project %s path=%s", project_id, project_path)

    return {"status": "ok", "message": f"Preprocessing started for project {project_id}"}


# ─── 6b. GET /api/projects/{project_id}/preprocess/status — 预处理状态快照 ─
@router.get("/api/projects/{project_id}/preprocess/status", tags=["项目管理"])
async def get_preprocess_status(project_id: str):
    """返回当前预处理进度（非 SSE，供 Tab 打开时加载）"""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    progress = await loop.run_in_executor(None, _app.store.get_progress, project_id)
    project = await loop.run_in_executor(None, _app.store.get_project, project_id)
    return {
        "project_status": project.get("status") if project else None,
        "progress": progress,
    }


# ─── 6. GET /api/projects/{project_id}/preprocess/progress — SSE 预处理进度流 ─
@router.get("/api/projects/{project_id}/preprocess/progress", tags=["项目管理"])
async def stream_preprocess_progress(project_id: str):
    """SSE 流式推送项目预处理进度

    事件格式: event: progress, data: {phase, phase_progress, message, ...}
    当 phase 为 complete 或 error 时发送后关闭流。
    """

    async def event_generator():
        last_phase = None
        last_progress = -1.0
        idle_count = 0

        while True:
            loop = asyncio.get_running_loop()
            progress = await loop.run_in_executor(None, _app.store.get_progress, project_id)

            if progress is None:
                # 尚无进度记录 — 项目可能刚创建
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "phase": "idle",
                        "phase_progress": 0.0,
                        "message": "Waiting for preprocessing to start...",
                    }),
                }
                idle_count += 1
                if idle_count > 60:  # 等待 60 秒仍无记录则关闭
                    yield {
                        "event": "progress",
                        "data": json.dumps({
                            "phase": "error",
                            "phase_progress": 0.0,
                            "message": "Preprocessing did not start within timeout",
                            "error": "timeout",
                        }),
                    }
                    return
                await asyncio.sleep(1.0)
                continue

            phase = progress.get("phase", "idle")
            phase_progress = progress.get("phase_progress", 0.0)

            # 只在状态变化时推送（减少冗余事件）
            if phase != last_phase or abs(phase_progress - last_progress) > 0.01:
                yield {
                    "event": "progress",
                    "data": json.dumps(progress, default=str),
                }
                last_phase = phase
                last_progress = phase_progress

            # 终止条件
            if phase in ("complete", "error"):
                return

            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())
