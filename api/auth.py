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
    "/api/status",
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
)


def _extract_token(request: Request) -> str:
    provided = request.headers.get("X-Swarm-Token") or request.headers.get("X-Swarm-Key") or ""
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    # 兜底：浏览器原生 EventSource(SSE) 无法携带自定义请求头，
    # 故 SSE 端点通过 query param ?token= 传递（header 优先级更高）。
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
