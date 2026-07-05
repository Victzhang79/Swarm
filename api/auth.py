"""可选鉴权 — 多用户 Token + 遗留 SWARM_API_KEY。"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from swarm.auth.rbac import Role
from swarm.auth.store import SwarmUser, get_user_by_token
from swarm.config.settings import get_config

logger = logging.getLogger(__name__)

_PUBLIC_PREFIXES = (
    "/api/health",
    # #21：/api/status 暴露 8 个组件(含 PostgreSQL/Qdrant/远程沙箱)健康拓扑给匿名调用方=基建信息泄露。
    # 移出公开前缀 → 走鉴权(前端轮询本就持 token)。存活探针只留 /api/health(无组件细节)。
    "/api/auth/login",
    "/static",
    "/docs",
    "/openapi.json",
    "/redoc",
)

_LEGACY_USER = SwarmUser(
    id="legacy-api-key",
    username="api-key",
    display_name="API Key User",
    global_role=Role.ADMIN.value,
    must_change_password=False,
)


def _extract_token(request: Request) -> str:
    provided = request.headers.get("X-Swarm-Token") or request.headers.get("X-Swarm-Key") or ""
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    # D1：HttpOnly Cookie（/api/auth/login 下发）。浏览器 EventSource(SSE) 同源自动带 Cookie，
    # 故 Cookie 认证的 SSE【无需】把 token 放进 ?token= URL（避免进 access log/Referer/浏览器
    # 历史致跨用户凭据泄漏）。优先级：显式 header > HttpOnly Cookie > ?token=(遗留兜底)。
    if not provided:
        provided = request.cookies.get("swarm_token") or ""
    # 遗留兜底：浏览器原生 EventSource 无法携带自定义请求头且未走 Cookie 时，
    # 仍支持 query param ?token=（安全性最弱，前端迁移到 Cookie 后可弃用）。
    if not provided:
        provided = request.query_params.get("token") or ""
    return provided.strip()


def resolve_user(token: str) -> SwarmUser | None:
    cfg = get_config()
    if not token:
        return None
    user = get_user_by_token(token)
    if user:
        return user
    legacy = (cfg.api_key or "").strip()
    if legacy and token == legacy:
        return _LEGACY_USER
    return None


def _extract_token_ws(websocket) -> str:
    """从 WebSocket 提取 token（header 优先，其次 ?token= query）。"""
    provided = (
        websocket.headers.get("x-swarm-token")
        or websocket.headers.get("x-swarm-key")
        or ""
    )
    if not provided:
        auth = websocket.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    if not provided:
        provided = websocket.query_params.get("token") or ""
    return provided.strip()


def authenticate_ws(websocket) -> SwarmUser | None:
    """P0-SEC-NEW：WebSocket 独立鉴权。

    BaseHTTPMiddleware 不处理 WebSocket scope → WS 端点必须自行校验 token，否则任何人
    无需 token 即可订阅任意 task_id 执行流。返回 user；rbac 关闭时返回 dev admin（与
    HTTP 中间件一致）；token 无效/缺失返回 None（调用方应关闭连接）。
    """
    cfg = get_config()
    if not cfg.rbac_enabled:
        return SwarmUser(
            id="anonymous",
            username="dev",
            display_name="Dev",
            global_role=Role.ADMIN.value,
            must_change_password=False,
        )
    return resolve_user(_extract_token_ws(websocket))


class SwarmAuthMiddleware(BaseHTTPMiddleware):
    """解析 Bearer / X-Swarm-Token；未配置 RBAC 时允许匿名 admin。"""

    async def dispatch(self, request: Request, call_next):
        cfg = get_config()
        path = request.url.path

        if path == "/" or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            request.state.user = None
            return await call_next(request)

        if not cfg.rbac_enabled:
            request.state.user = SwarmUser(
                id="anonymous",
                username="dev",
                display_name="Dev",
                global_role=Role.ADMIN.value,
                must_change_password=False,
            )
            return await call_next(request)

        token = _extract_token(request)
        user = resolve_user(token)
        if user is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing auth token. POST /api/auth/login"},
            )

        request.state.user = user
        return await call_next(request)


# 向后兼容旧名
SwarmAPIKeyMiddleware = SwarmAuthMiddleware
