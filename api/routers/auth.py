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
    from swarm.auth.store import authenticate, get_must_change_password

    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, lambda: authenticate(req.username, req.password))
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    # 12.19：默认弱密码的 admin 需强制改密。RBAC 关闭(开发/CI)时仍返回标志，
    # 但前端不阻断——保证开箱即用与 CI 的 admin/swarm 登录不受影响。
    must_change = await loop.run_in_executor(
        None, lambda: get_must_change_password(user.id)
    )
    return {
        "token": user.api_token,
        "must_change_password": must_change,
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "global_role": user.global_role,
        },
    }


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/api/auth/change-password", tags=["认证"])
async def auth_change_password(request: Request, req: ChangePasswordRequest):
    """当前登录用户修改自己的密码（12.19）：校验旧密码→更新→清除强制改密标志。"""
    user = _require_user(request)
    if not req.new_password or len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if req.new_password == req.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")

    from swarm.auth.store import (
        authenticate,
        clear_must_change_password,
        update_user_password,
    )

    loop = asyncio.get_running_loop()
    # 用旧密码校验身份（authenticate 按用户名+密码）
    verified = await loop.run_in_executor(
        None, lambda: authenticate(user.username, req.old_password)
    )
    if verified is None:
        raise HTTPException(status_code=401, detail="旧密码不正确")

    def _apply() -> None:
        update_user_password(user.id, req.new_password)
        clear_must_change_password(user.id)

    await loop.run_in_executor(None, _apply)
    return {"ok": True}


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
