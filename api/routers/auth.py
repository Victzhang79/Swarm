"""api/routers/auth.py — 认证域路由 (登录/当前用户/用户管理/成员管理)。

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(_validate_project)用 _app. 属性访问保测试零改动。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

import swarm.api.app as _app
from swarm.api._shared import _require_perm, _require_user

router = APIRouter()


class _LoginThrottle:
    """M8：登录失败限流/锁定（进程内内存，多副本部署可后续换 Redis）。

    每个 key（用户名|IP）维护失败时间戳列表；窗口内失败 >= 阈值则锁定。
    成功登录清空该 key。线程安全（authenticate 在 executor 线程跑，但本类操作很轻，用锁守护）。
    """

    def __init__(self, max_failures: int = 5, window_sec: int = 300, lockout_sec: int = 300):
        self.max_failures = max_failures
        self.window_sec = window_sec
        self.lockout_sec = lockout_sec
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        import threading
        self._lock = threading.Lock()

    def is_locked(self, key: str) -> tuple[bool, int]:
        import time
        now = time.time()
        with self._lock:
            until = self._locked_until.get(key, 0)
            if until > now:
                return True, int(until - now) + 1
            return False, 0

    def record_failure(self, key: str) -> None:
        import time
        now = time.time()
        with self._lock:
            stamps = [t for t in self._failures.get(key, []) if now - t < self.window_sec]
            stamps.append(now)
            self._failures[key] = stamps
            if len(stamps) >= self.max_failures:
                self._locked_until[key] = now + self.lockout_sec
                self._failures[key] = []

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


_login_throttle = _LoginThrottle()


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

    @field_validator("role")
    @classmethod
    def _validate_project_role(cls, v: str) -> str:
        # C1 治本：项目成员角色必须在【项目级可分配角色】白名单内。旧代码不校验 → owner 可写
        # role="admin"（或任意 ROLE_PERMISSIONS 键），effective_project_role 采信 member_role、
        # admin 的 "*" 通配符恒真 → 项目级通配符提权。排除 admin（全局角色，不经成员接口授予）。
        from swarm.auth.rbac import PROJECT_ASSIGNABLE_ROLES
        if v not in PROJECT_ASSIGNABLE_ROLES:
            raise ValueError(
                f"非法项目角色 '{v}'，仅允许 {sorted(PROJECT_ASSIGNABLE_ROLES)}（admin 为全局角色，不可经成员接口授予）"
            )
        return v


@router.post("/api/auth/login", tags=["认证"])
async def auth_login(req: LoginRequest, request: Request):
    """用户名密码登录，返回 api_token（Bearer / X-Swarm-Token）。"""
    from swarm.auth.store import authenticate, get_must_change_password

    # M8 修复：登录限流/锁定，防默认账户暴力破解。按 用户名+客户端IP 计失败次数，
    # 超阈值在锁定窗口内直接 429，避免无限尝试。
    client_ip = request.client.host if request.client else "unknown"
    throttle_key = f"{req.username}|{client_ip}"
    locked, retry_after = _login_throttle.is_locked(throttle_key)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )

    loop = asyncio.get_running_loop()
    user = await loop.run_in_executor(None, lambda: authenticate(req.username, req.password))
    if user is None:
        _login_throttle.record_failure(throttle_key)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _login_throttle.record_success(throttle_key)
    # 12.19：默认弱密码的 admin 需强制改密。RBAC 关闭(开发/CI)时仍返回标志，
    # 但前端不阻断——保证开箱即用与 CI 的 admin/swarm 登录不受影响。
    must_change = await loop.run_in_executor(
        None, lambda: get_must_change_password(user.id)
    )
    # W3.1：按配置 TTL 刷新 token 有效期（滑动续期），并回传 expires_at 供前端到期提示。
    # token_ttl_hours=0 时返回 None（永不过期），保持既有行为。
    from swarm.auth.store import set_token_expiry
    from swarm.config.settings import get_config

    ttl_hours = get_config().token_ttl_hours
    expires_at = await loop.run_in_executor(
        None, lambda: set_token_expiry(user.id, ttl_hours)
    )
    return {
        "token": user.api_token,
        "must_change_password": must_change,
        "expires_at": expires_at,  # ISO8601 或 null(永不过期)
        "token_ttl_hours": ttl_hours,
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
        "must_change_password": getattr(user, "must_change_password", False),
    }


@router.get("/api/users", tags=["认证"])
async def list_users_api(request: Request):
    _require_perm(request, "config:write")
    from swarm.auth.store import list_users

    loop = asyncio.get_running_loop()
    return {"users": await loop.run_in_executor(None, list_users)}


@router.post("/api/users", tags=["认证"])
async def create_user_api(request: Request, req: CreateUserRequest):
    caller = _require_perm(request, "config:write")
    # 防提权：铸造特权角色（admin/owner）要求调用方本身是全局 admin。
    # 否则仅有 config:write 的 OWNER 可凭空造出全局 admin（越权升级）。
    if str(req.global_role).lower() in ("admin", "owner") and \
            str(getattr(caller, "global_role", "")).lower() != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only a global admin may create admin/owner users",
        )
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


@router.delete("/api/projects/{project_id}/members/{user_id}", tags=["认证"])
async def remove_member_api(project_id: str, user_id: str, request: Request):
    """移除项目成员（需 member:manage 权限：项目 owner / admin）。"""
    _require_perm(request, "member:manage", project_id)
    from swarm.auth.store import remove_project_member

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _app._validate_project, project_id)
    removed = await loop.run_in_executor(
        None, lambda: remove_project_member(project_id, user_id)
    )
    if not removed:
        raise HTTPException(status_code=404, detail="该用户不是项目成员")
    return {"project_id": project_id, "user_id": user_id, "removed": True}
