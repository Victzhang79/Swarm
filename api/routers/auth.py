"""api/routers/auth.py — 认证域路由 (登录/当前用户/用户管理/成员管理)。

从 api/app.py 抽出, app.include_router 挂载。
mock 锚点(_validate_project)用 _app. 属性访问保测试零改动。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

import swarm.api.app as _app
from swarm.api._shared import _require_perm, _require_user

router = APIRouter()


def _real_client_ip(request: Request) -> str:
    """B8-F5：解析真实 client IP 用于登录限流键。

    反代（nginx/ingress）后 request.client.host 是全体用户共享的 proxy IP → 登录限流键塌成
    一个 IP：(a) 暴破防护失效（全体计一个桶），(b) 第三方用受害者 username 连续失败即锁
    `username|proxyIP`，受害者同走该 proxy 被账户锁定 DoS。

    按 SWARM_TRUSTED_PROXY_HOPS(N) 从 X-Forwarded-For 取真实 client：我方 N 个可信代理会各自
    append 上游 TCP peer，故 XFF 最右 N 个条目可信，真实 client = 第 (len-N) 个（其左侧任何条目
    都是客户端可伪造的，一律不取）。fail-closed：未配可信代理（默认 0）→ XFF 完全不可信 → 退回
    request.client.host（既有行为，无回归）；链短于 N 跳（异常/绕过）→ 同样退回直连 peer。

    ⚠️ 运维前提（对抗复核 MEDIUM）：SWARM_TRUSTED_PROXY_HOPS>0 只应在【源站端口对外网防火墙隔离、
    仅可信反代可直连】时设置。本函数不校验直连 TCP peer(request.client.host)是否真为可信代理 IP——
    若源站可被外网直连绕过反代，攻击者自造恰好 N 段 XFF 即可完全操纵限流键。默认 0 不受影响。

    对抗复核 HIGH：用 getlist 而非 get——同名 X-Forwarded-For 出现多条 header 行时（部分 LB/网关
    以【追加独立 header 行】而非单行 CSV 拼接转发），Headers.get() 只返回第一条=攻击者自发的伪造行，
    可信代理追加的真实行被忽略 → 伪造前缀绕过原样复现。按 RFC 7230 §3.2.2 把多条实例 join 成整体解析。
    """
    import os

    direct = request.client.host if request.client else "unknown"
    try:
        hops = int((os.environ.get("SWARM_TRUSTED_PROXY_HOPS") or "0").strip())
    except ValueError:
        hops = 0
    if hops <= 0:
        return direct
    xff = ",".join(request.headers.getlist("x-forwarded-for"))
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    idx = len(parts) - hops
    if idx < 0 or idx >= len(parts):
        return direct
    return parts[idx]


def _issue_token_cookie(response: Response, request: Request, token: str) -> None:
    """把 token 以 HttpOnly Cookie 下发/续期 —— 登录与 /api/auth/me 引导【共用单一事实源】。

    D1：供浏览器原生 EventSource(SSE) 同源【自动携带】鉴权，无需把 token 放进 ?token= URL
    （会进 access log/Referer/浏览器历史 → 多用户下跨用户凭据泄漏）。
    参数须与 /api/auth/logout 的 delete_cookie 对齐（key/path/samesite/secure/httponly），否则
    浏览器不认作同一 Cookie → 清不掉/续不上。token_ttl_hours=0/None → 会话 Cookie（max_age=None）。
    """
    from swarm.config.settings import get_config

    ttl_hours = get_config().token_ttl_hours
    response.set_cookie(
        key="swarm_token",
        value=token,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        max_age=(ttl_hours * 3600 if ttl_hours and ttl_hours > 0 else None),
        path="/",
    )


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
async def auth_login(req: LoginRequest, request: Request, response: Response):
    """用户名密码登录，返回 api_token（Bearer / X-Swarm-Token）。

    D1：同时以 HttpOnly Cookie 下发 token，供 SSE(EventSource)同源自动携带，避免把 token
    放进 ?token= URL(会进 access log/Referer/浏览器历史 → 多用户下跨用户凭据泄漏)。
    """
    from swarm.auth.store import authenticate, get_must_change_password

    # M8 修复：登录限流/锁定，防默认账户暴力破解。按 用户名+客户端IP 计失败次数，
    # 超阈值在锁定窗口内直接 429，避免无限尝试。
    client_ip = _real_client_ip(request)  # B8-F5：反代后取真实 client IP（可信代理跳数门控）
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
    # F1：token hash-at-rest 后服务端不再存明文，登录必须【铸造新 token】才能把明文交给客户端。
    # 轮换即产生新明文（旧 token 随之失效——安全正向），下发 Cookie/响应体的都是这个新明文。
    from swarm.auth.store import rotate_user_token, set_token_expiry
    from swarm.config.settings import get_config

    new_token = await loop.run_in_executor(None, lambda: rotate_user_token(user.id))
    # W3.1：按配置 TTL 刷新 token 有效期（滑动续期），并回传 expires_at 供前端到期提示。
    # token_ttl_hours=0 时返回 None（永不过期），保持既有行为。轮换已清 revoked，此处设 expiry。
    ttl_hours = get_config().token_ttl_hours
    expires_at = await loop.run_in_executor(
        None, lambda: set_token_expiry(user.id, ttl_hours)
    )
    # D1：HttpOnly Cookie 下发 token（JS 读不到 → 不受 XSS 窃取、不进 URL）。见 _issue_token_cookie。
    _issue_token_cookie(response, request, new_token)
    return {
        "token": new_token,
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


@router.post("/api/auth/logout", tags=["认证"])
async def auth_logout(request: Request, response: Response):
    """登出：清除 HttpOnly swarm_token Cookie。

    D1 治本（伪退出/auth bypass）：登录时 set_cookie 下发了 HttpOnly Cookie，但前端 logoutUser()
    只清 localStorage → 退出后同源请求仍带 swarm_token Cookie，_extract_token 在 header 为空时
    回退 Cookie 会继续鉴权通过。故必须在服务端 delete_cookie 把凭据从浏览器清掉。

    不要求已鉴权：清 Cookie 幂等、恒成功（即便 token 已失效也要把 Cookie 清干净）。参数须与
    /api/auth/login 的 set_cookie 对齐（key/path/samesite/secure/httponly），否则浏览器不认作
    同一 Cookie、不会删除。注：api_token 是持久 API 凭据（Bearer 复用），此处只清传输层 Cookie，
    不轮换 token（避免连坐失效该用户其它 Bearer 会话）。
    """
    response.delete_cookie(
        key="swarm_token",
        path="/",
        samesite="lax",
        secure=(request.url.scheme == "https"),
        httponly=True,
    )
    return {"status": "ok"}


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/api/auth/change-password", tags=["认证"])
async def auth_change_password(request: Request, req: ChangePasswordRequest, response: Response):
    """当前登录用户修改自己的密码（12.19）：校验旧密码→更新→清除强制改密标志。

    D19：改密即吊销该用户既有 token（update_user_password 内原子完成）——被盗 token
    改密后立即失效。当前会话语义与登录一致（F1 登录必轮换）：改密成功后轮换出新 token，
    经响应体 + HttpOnly Cookie 下发，本会话无缝续用；其它已缓存旧 token 的会话失效需重登录。
    """
    user = _require_user(request)
    if not req.new_password or len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    if req.new_password == req.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与旧密码相同")

    from swarm.auth.store import (
        authenticate,
        clear_must_change_password,
        rotate_user_token,
        set_token_expiry,
        update_user_password,
    )
    from swarm.config.settings import get_config

    loop = asyncio.get_running_loop()
    # 用旧密码校验身份（authenticate 按用户名+密码）
    verified = await loop.run_in_executor(
        None, lambda: authenticate(user.username, req.old_password)
    )
    if verified is None:
        raise HTTPException(status_code=401, detail="旧密码不正确")

    def _apply() -> str:
        update_user_password(user.id, req.new_password)   # D19：内含 token_revoked=true
        clear_must_change_password(user.id)
        # 与登录同语义：铸新 token（旧 hash 被替换、revoked 清除），本会话续命。
        new_token = rotate_user_token(user.id)
        set_token_expiry(user.id, get_config().token_ttl_hours)
        return new_token

    new_token = await loop.run_in_executor(None, _apply)
    _issue_token_cookie(response, request, new_token)
    return {"ok": True, "token": new_token}


@router.get("/api/auth/me", tags=["认证"])
async def auth_me(request: Request, response: Response):
    user = _require_user(request)
    # D1：boot/自动登录路径（前端启动即调 /api/auth/me 校验 localStorage token）顺带【续发】
    # HttpOnly Cookie，使 SSE cookie 鉴权在浏览器重启丢失【会话 Cookie】(ttl=0)后仍可用——否则
    # localStorage token 永不过期但会话 Cookie 已失效 → SSE 401、REST 却仍靠 header 通，割裂。
    # F1：token hash-at-rest 后 user.api_token 恒 NULL（服务端不再存明文）。改从【本次请求携带的
    # token】续发 Cookie——客户端已持有明文（Bearer header 或既有 Cookie），此处只是刷新 Cookie
    # 传输层，不铸新、不触碰库。无 token（legacy api-key/无凭据）则跳过。
    from swarm.api.auth import _extract_token
    _tok = _extract_token(request)
    if _tok:
        _issue_token_cookie(response, request, _tok)
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
    # D5：global_role 白名单——只接受已知角色，拒绝任意字符串(未知角色 RBAC 行为未定义/
    # 可能默认放行，是提权面)。
    from swarm.auth.rbac import Role
    _valid_roles = {r.value for r in Role}
    _role = str(req.global_role).lower().strip()
    if _role not in _valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"无效 global_role：{req.global_role}（合法值：{sorted(_valid_roles)}）",
        )
    # 防提权：铸造特权角色（admin/owner）要求调用方本身是全局 admin。
    # 否则仅有 config:write 的 OWNER 可凭空造出全局 admin（越权升级）。
    if _role in ("admin", "owner") and \
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


@router.post("/api/users/{user_id}/revoke-token", tags=["认证"])
async def revoke_token_api(user_id: str, request: Request):
    """F9：吊销指定用户的 API token（泄露应急）。需全局 admin。

    `revoke_user_token`（auth/store.py）此前已实现但无路由触发——补上端点让"泄露即失效"能力可用。
    吊销后该 token 立即认证失败；合法用户重新登录会轮换出新 token（F1）并自动清除 revoked 标志。
    """
    caller = _require_perm(request, "config:write")
    # 吊销他人凭据是高权操作：要求调用方本身是全局 admin（与铸造特权用户同规格）。
    if str(getattr(caller, "global_role", "")).lower() != "admin":
        raise HTTPException(status_code=403, detail="Only a global admin may revoke tokens")
    from swarm.auth.store import revoke_user_token

    loop = asyncio.get_running_loop()
    revoked = await loop.run_in_executor(None, lambda: revoke_user_token(user_id))
    if not revoked:
        raise HTTPException(status_code=404, detail="用户不存在或无可吊销的 token")
    return {"user_id": user_id, "revoked": True}


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
