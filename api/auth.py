"""可选鉴权 — 多用户 Token + 遗留 SWARM_API_KEY。"""

from __future__ import annotations

import hmac
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
)

# CODEWALK P1-3：docs 类端点暴露【全量 API schema】——生产+RBAC 下不应匿名可读（与 #21
# 收 /api/status 同一动机）。非生产保持公开（本地开发调试零摩擦）；生产默认纳入鉴权
#（持 token 仍可访问，非一刀切禁用）；SWARM_DOCS_PUBLIC=true/false 显式覆盖两个方向。
_DOCS_PREFIXES = ("/docs", "/openapi.json", "/redoc")


def _docs_public() -> bool:
    import os

    v = (os.environ.get("SWARM_DOCS_PUBLIC") or "").strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    try:
        return not get_config().is_production()
    except Exception:  # noqa: BLE001
        # 配置读取失败不许把安全判定炸成 500（非 401 非 200 的 fail-undefined）——
        # fail-closed：当生产处理（docs 落入常规鉴权），并留可诊断日志。
        logger.warning("_docs_public: get_config() 失败，按生产收权处理（fail-closed）")
        return False

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
    # 故 Cookie 认证的 SSE【无需】把 token 放进 ?token= URL。优先级：显式 header > HttpOnly Cookie。
    if not provided:
        provided = request.cookies.get("swarm_token") or ""
    # F1：★已关闭 ?token= URL 兜底★——token 进 URL 会落 access log/Referer/浏览器历史，多用户下
    # 是跨用户凭据泄漏面。浏览器 EventSource/SSE 走 HttpOnly Cookie（同源自动携带），CLI/程序走
    # Bearer/X-Swarm-Token header，两路已全覆盖，无需 URL 兜底。
    return provided.strip()


def resolve_user(token: str) -> SwarmUser | None:
    cfg = get_config()
    if not token:
        return None
    user = get_user_by_token(token)
    if user:
        return user
    legacy = (cfg.api_key or "").strip()
    # D47a：常量时间比较——`==` 短路于首个不等字节，攻击者可按响应时延逐字节爆破 legacy key。
    if legacy and hmac.compare_digest(token.encode("utf-8"), legacy.encode("utf-8")):
        return _LEGACY_USER
    return None


def _extract_token_ws(websocket) -> str:
    """从 WebSocket 提取 token。优先级：显式 header > HttpOnly Cookie > ?token=(遗留兜底)。

    F13-WS：补上 HttpOnly Cookie 读取——浏览器 WebSocket 握手是同源 HTTP GET upgrade，会【自动
    携带】同源 Cookie（含 HttpOnly），故浏览器 WS 无需再把 token 塞进 ?token= URL（与 SSE 同源
    自动带 Cookie 同理）。?token= 仅为无 Cookie/无 header 的程序化 WS 客户端保留的最弱兜底。
    """
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
        provided = websocket.cookies.get("swarm_token") or ""
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

        # docs 类端点：非生产（或显式放开）才匿名公开；生产默认落入下方常规鉴权
        if any(path.startswith(p) for p in _DOCS_PREFIXES) and _docs_public():
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
