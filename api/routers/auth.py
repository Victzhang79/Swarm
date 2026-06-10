"""api/routers/auth.py — 认证域路由 (登录/当前用户/用户管理/成员管理)。

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(_validate_project)用 _app. 属性访问保测试零改动。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import swarm.api.app as _app
from swarm.api._shared import _require_perm, _require_user

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    global_role: str = "developer"


class MemberRequest(BaseModel):
    user_id: str
    role: str = "developer"


@router.post("/api/auth/login", tags=["认证"])
async def auth_login(req: LoginRequest):
    """用户名密码登录，返回 api_token（Bearer / X-Swarm-Token）。"""
    from swarm.auth.store import authenticate

    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, lambda: authenticate(req.username, req.password))
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {
        "token": user.api_token,
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "global_role": user.global_role,
        },
    }


@router.get("/api/auth/me", tags=["认证"])
async def auth_me(request: Request):
    user = _require_user(request)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "global_role": user.global_role,
    }


@router.get("/api/users", tags=["认证"])
async def list_users_api(request: Request):
    _require_perm(request, "config:write")
    from swarm.auth.store import list_users

    loop = asyncio.get_running_loop()
    return {"users": await loop.run_in_executor(None, list_users)}


@router.post("/api/users", tags=["认证"])
async def create_user_api(request: Request, req: CreateUserRequest):
    _require_perm(request, "config:write")
    from swarm.auth.store import create_user

    loop = asyncio.get_running_loop()

    def _create():
        try:
            return create_user(
                username=req.username,
                password=req.password,
                display_name=req.display_name or None,
                global_role=req.global_role,
            )
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=409, detail="Username already exists") from exc
            raise

    user = await loop.run_in_executor(None, _create)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "global_role": user.global_role,
        "token": user.api_token,
    }


@router.get("/api/projects/{project_id}/members", tags=["认证"])
async def list_members_api(project_id: str, request: Request):
    _require_perm(request, "project:read", project_id)
    from swarm.auth.store import list_project_members

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    members = await loop.run_in_executor(None, lambda: list_project_members(project_id))
    return {"members": members}


@router.put("/api/projects/{project_id}/members", tags=["认证"])
async def set_member_api(project_id: str, req: MemberRequest, request: Request):
    _require_perm(request, "member:manage", project_id)
    from swarm.auth.store import set_project_member

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    await loop.run_in_executor(
        None,
        lambda: set_project_member(project_id, req.user_id, req.role),
    )
    return {"project_id": project_id, "user_id": req.user_id, "role": req.role}
