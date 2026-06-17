"""api/routers/memory.py — 记忆域路由 (错题集/成功模式/任务摘要/用户画像)。

从 api/app.py 抽出。通过 app.include_router(router) 挂载。

测试合约: 路由体对 _validate_project / _get_pg_conn 使用 `_app.` 属性访问,
确保现有 patch("swarm.api.app._validate_project") 等 mock 继续生效。
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg as _psycopg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import swarm.api.app as _app
from swarm.api._shared import (
    _EMBEDDING_ZERO,
    _profile_storage_key,
    _require_perm,
)

router = APIRouter()


class MistakeCreateRequest(BaseModel):
    """添加错题请求"""
    task_id: str = Field(default="", description="关联任务ID")
    error_type: str = Field(description="错误类型")
    description: str = Field(description="错误描述")
    context: str | None = Field(default=None, description="出错上下文")
    fix_description: str | None = Field(default=None, description="修复方式")


class SuccessCreateRequest(BaseModel):
    """添加成功模式请求"""
    task_id: str = Field(default="", description="关联任务ID")
    pattern_name: str = Field(description="模式名称")
    description: str | None = Field(default=None, description="模式描述")
    approach: str | None = Field(default=None, description="成功方案")
    applicable_when: str | None = Field(default=None, description="适用条件")


class SummaryCreateRequest(BaseModel):
    """添加任务摘要请求"""
    task_id: str = Field(description="任务 ID")
    summary: str = Field(description="摘要内容")
    outcome: str | None = Field(default=None, description="结果: success/failure/partial")
    lessons_learned: str | None = Field(default=None, description="经验教训")


class SummaryUpdateRequest(BaseModel):
    """编辑任务摘要请求 — 只更新提供的字段"""
    summary: str | None = Field(default=None, description="摘要内容")
    outcome: str | None = Field(default=None, description="结果")
    lessons_learned: str | None = Field(default=None, description="经验教训")


class ProfileUpdateRequest(BaseModel):
    """更新 L1 用户画像请求"""
    profile_json: dict[str, Any] = Field(default_factory=dict, description="用户画像 JSON")


# ─── 知识库 — 概览 ────────────────────────────────


# ─── 记忆 — 错题集 (mem_mistakes) ────────────────



@router.get("/api/projects/{project_id}/memories/mistakes", tags=["记忆"])
async def list_mistakes(project_id: str, request: Request):
    """获取项目错题列表"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03：防跨项目读
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _query():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, project_id, task_id, error_type, description, context, "
                    "fix_description, decay_weight, occurrence_count, created_at "
                    "FROM mem_mistakes WHERE project_id = %s ORDER BY created_at DESC",
                    (project_id,),
                )
                cols = ["id", "project_id", "task_id", "error_type", "description", "context",
                        "fix_description", "decay_weight", "occurrence_count", "created_at"]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    mistakes = await loop.run_in_executor(None, _query)
    return {"mistakes": mistakes}


@router.post("/api/projects/{project_id}/memories/mistakes", tags=["记忆"])
async def create_mistake(project_id: str, request: Request, req: MistakeCreateRequest):
    """添加错题"""
    _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _insert():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mem_mistakes "
                    "(project_id, task_id, error_type, description, context, fix_description, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (project_id, req.task_id or "", req.error_type, req.description,
                     req.context, req.fix_description, _EMBEDDING_ZERO),
                )
                row = cur.fetchone()
                return {"id": row[0]}

    return await loop.run_in_executor(None, _insert)


@router.post("/api/projects/{project_id}/memories/mistakes/{mid}/dismiss", tags=["记忆"])
async def dismiss_mistake(project_id: str, mid: int, request: Request):
    """标记错题为已修复/归档（检索降权，不物理删除）"""
    _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    async def _do_dismiss():
        from swarm.memory.store import MemoryStore

        store = MemoryStore()
        await store.connect()
        try:
            ok = await store.dismiss_mistake(project_id, mid)
            if not ok:
                raise HTTPException(status_code=404, detail=f"Mistake {mid} not found")
            return {"dismissed": True, "id": mid}
        finally:
            await store.close()

    return await _do_dismiss()


@router.delete("/api/projects/{project_id}/memories/mistakes/{mid}", tags=["记忆"])
async def delete_mistake(project_id: str, mid: int, request: Request):
    """删除错题"""
    _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _do_delete():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mem_mistakes WHERE project_id = %s AND id = %s",
                    (project_id, mid),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail=f"Mistake {mid} not found")
        return {"deleted": True}

    return await loop.run_in_executor(None, _do_delete)


# ─── 记忆 — 成功模式 (mem_successes) ──────────────


@router.get("/api/projects/{project_id}/memories/successes", tags=["记忆"])
async def list_successes(project_id: str, request: Request):
    """获取项目成功模式列表"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03：防跨项目读
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _query():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, project_id, task_id, pattern_name, description, approach, "
                    "applicable_when, reuse_count, created_at "
                    "FROM mem_successes WHERE project_id = %s ORDER BY created_at DESC",
                    (project_id,),
                )
                cols = ["id", "project_id", "task_id", "pattern_name", "description", "approach",
                        "applicable_when", "reuse_count", "created_at"]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    successes = await loop.run_in_executor(None, _query)
    return {"successes": successes}


@router.post("/api/projects/{project_id}/memories/successes", tags=["记忆"])
async def create_success(project_id: str, request: Request, req: SuccessCreateRequest):
    """添加成功模式"""
    _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _insert():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mem_successes "
                    "(project_id, task_id, pattern_name, description, approach, applicable_when, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (project_id, req.task_id or "", req.pattern_name, req.description,
                     req.approach, req.applicable_when, _EMBEDDING_ZERO),
                )
                row = cur.fetchone()
                return {"id": row[0]}

    return await loop.run_in_executor(None, _insert)


@router.delete("/api/projects/{project_id}/memories/successes/{sid}", tags=["记忆"])
async def delete_success(project_id: str, sid: int, request: Request):
    """删除成功模式"""
    _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _do_delete():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mem_successes WHERE project_id = %s AND id = %s",
                    (project_id, sid),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail=f"Success {sid} not found")
        return {"deleted": True}

    return await loop.run_in_executor(None, _do_delete)


# ─── 记忆 — 任务摘要 (mem_task_summary) ──────────


@router.get("/api/projects/{project_id}/memories/summaries", tags=["记忆"])
async def list_summaries(project_id: str, request: Request):
    """获取项目任务摘要列表"""
    _require_perm(request, "project:read", project_id)  # P0-SEC-03：防跨项目读
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _query():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, project_id, task_id, summary, outcome, lessons_learned, created_at "
                    "FROM mem_task_summary WHERE project_id = %s ORDER BY created_at DESC",
                    (project_id,),
                )
                cols = ["id", "project_id", "task_id", "summary", "outcome", "lessons_learned", "created_at"]
                return [dict(zip(cols, row)) for row in cur.fetchall()]

    summaries = await loop.run_in_executor(None, _query)
    return {"summaries": summaries}


@router.post("/api/projects/{project_id}/memories/summaries", tags=["记忆"])
async def create_summary(project_id: str, request: Request, req: SummaryCreateRequest):
    """添加任务摘要"""
    _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _insert():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mem_task_summary "
                    "(project_id, task_id, summary, outcome, lessons_learned) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (project_id, req.task_id, req.summary, req.outcome, req.lessons_learned),
                )
                row = cur.fetchone()
                return {"id": row[0]}

    return await loop.run_in_executor(None, _insert)


@router.put("/api/projects/{project_id}/memories/summaries/{sid}", tags=["记忆"])
async def update_summary(project_id: str, sid: int, request: Request, req: SummaryUpdateRequest):
    """编辑任务摘要 — 只更新提供的字段"""
    _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    def _do_update():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                set_clause = ", ".join(f"{k} = %s" for k in updates)
                values = list(updates.values()) + [project_id, sid]
                cur.execute(
                    f"UPDATE mem_task_summary SET {set_clause} "
                    f"WHERE project_id = %s AND id = %s",
                    values,
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail=f"Summary {sid} not found")
        return {"updated": True}

    return await loop.run_in_executor(None, _do_update)


@router.delete("/api/projects/{project_id}/memories/summaries/{sid}", tags=["记忆"])
async def delete_summary(project_id: str, sid: int, request: Request):
    """删除任务摘要"""
    _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)

    def _do_delete():
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mem_task_summary WHERE project_id = %s AND id = %s",
                    (project_id, sid),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail=f"Summary {sid} not found")
        return {"deleted": True}

    return await loop.run_in_executor(None, _do_delete)


# ─── 记忆 — L1 用户画像 (mem_user_profile) ───────




@router.get("/api/projects/{project_id}/memories/profile", tags=["记忆"])
async def get_memory_profile(project_id: str, request: Request):
    """获取当前用户在项目下的 L1 画像"""
    user = _require_perm(request, "memory:read", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    storage_key = _profile_storage_key(user.id, project_id)

    def _query() -> dict[str, Any]:
        from swarm.auth.default_profile import GLOBAL_PROFILE_SUFFIX
        from swarm.auth.store import profile_key

        global_key = profile_key(user.id, GLOBAL_PROFILE_SUFFIX)
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT profile_json FROM mem_user_profile WHERE user_id = %s",
                    (storage_key,),
                )
                row = cur.fetchone()
                if row:
                    profile = row[0]
                else:
                    cur.execute(
                        "SELECT profile_json FROM mem_user_profile WHERE user_id = %s",
                        (global_key,),
                    )
                    global_row = cur.fetchone()
                    if global_row:
                        profile = global_row[0]
                    else:
                        cur.execute(
                            "SELECT profile_json FROM mem_user_profile WHERE user_id = %s",
                            (project_id,),
                        )
                        legacy = cur.fetchone()
                        profile = legacy[0] if legacy else {}
        if not isinstance(profile, dict):
            profile = {}
        return profile

    profile_json = await loop.run_in_executor(None, _query)
    return {
        "user_id": user.id,
        "project_id": project_id,
        "profile_json": profile_json,
    }


@router.put("/api/projects/{project_id}/memories/profile", tags=["记忆"])
async def update_memory_profile(project_id: str, req: ProfileUpdateRequest, request: Request):
    """更新当前用户在项目下的 L1 画像"""
    user = _require_perm(request, "memory:write", project_id)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    storage_key = _profile_storage_key(user.id, project_id)

    def _upsert() -> dict[str, Any]:
        with _app._get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mem_user_profile (user_id, profile_json, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (user_id) DO UPDATE SET
                        profile_json = EXCLUDED.profile_json,
                        updated_at   = now()
                    RETURNING profile_json
                    """,
                    (storage_key, _psycopg.types.json.Jsonb(req.profile_json)),
                )
                row = cur.fetchone()
        profile = row[0] if row else req.profile_json
        if not isinstance(profile, dict):
            profile = req.profile_json
        return profile

    profile_json = await loop.run_in_executor(None, _upsert)
    return {
        "user_id": user.id,
        "project_id": project_id,
        "profile_json": profile_json,
        "updated": True,
    }
