"""api/_shared.py — 跨域共享辅助。

从 api/app.py 抽出的、被多个路由域复用的工具函数与常量。
单一事实来源：路由模块统一从这里 import，避免循环依赖与重复定义。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from swarm.config.settings import AppConfig

# ─── 常量 ─────────────────────────────────────────

_API_KEY_PATTERN = re.compile(r"(sk-|key-|e2b_)(.{4,})(.{4,})", re.IGNORECASE)

_SHORT_KEY_MAP = {
    "siliconflow_api_key": "SWARM_MODEL_SILICONFLOW_API_KEY",
    "siliconflow_base_url": "SWARM_MODEL_SILICONFLOW_BASE_URL",
    "local_api_key": "SWARM_MODEL_LOCAL_API_KEY",
    "tier_enabled": "SWARM_MODEL_TIER_ENABLED",
    "tier": "SWARM_MODEL_TIER",
    "local_base_url": "SWARM_MODEL_LOCAL_BASE_URL",
    "brain_primary": "SWARM_MODEL_BRAIN_PRIMARY",
    "brain_fallback": "SWARM_MODEL_BRAIN_FALLBACK",
    "worker_primary": "SWARM_MODEL_WORKER_PRIMARY",
    "worker_local": "SWARM_MODEL_WORKER_LOCAL",
    "worker_fallback": "SWARM_MODEL_WORKER_FALLBACK",
    "brain_temperature": "SWARM_MODEL_BRAIN_TEMPERATURE",
    "worker_temperature": "SWARM_MODEL_WORKER_TEMPERATURE",
    "brain_model": "SWARM_MODEL_BRAIN_PRIMARY",   # 前端别名
    "worker_model": "SWARM_MODEL_WORKER_PRIMARY",  # 前端别名
    "routing_trivial": "SWARM_MODEL_ROUTING_TRIVIAL",
    "routing_trivial_fallback": "SWARM_MODEL_ROUTING_TRIVIAL_FALLBACK",
    "routing_medium": "SWARM_MODEL_ROUTING_MEDIUM",
    "routing_medium_fallback": "SWARM_MODEL_ROUTING_MEDIUM_FALLBACK",
    "routing_complex": "SWARM_MODEL_ROUTING_COMPLEX",
    "routing_complex_fallback": "SWARM_MODEL_ROUTING_COMPLEX_FALLBACK",
    "routing_multimodal": "SWARM_MODEL_ROUTING_MULTIMODAL",
    "routing_multimodal_fallback": "SWARM_MODEL_ROUTING_MULTIMODAL_FALLBACK",
    "langsmith_tracing": "SWARM_LANGSMITH_TRACING",
    "langsmith_api_key": "SWARM_LANGSMITH_API_KEY",
    "langsmith_project": "SWARM_LANGSMITH_PROJECT",
    "langsmith_endpoint": "SWARM_LANGSMITH_ENDPOINT",
    "sandbox_api_url": "SWARM_SANDBOX_API_URL",
    "sandbox_proxy_base": "SWARM_SANDBOX_PROXY_BASE",
    "sandbox_default_template": "SWARM_SANDBOX_DEFAULT_TEMPLATE",
    "sandbox_api_key": "SWARM_SANDBOX_API_KEY",
    "sandbox_use_for_worker": "SWARM_SANDBOX_USE_FOR_WORKER",
}

# ─── API Key 脱敏 ─────────────────────────────────────────

def _mask_api_key(value: str) -> str:
    """脱敏 API Key: sk-xxxx...xxxx (只显示前4后4位)"""
    if not value or len(value) < 12:
        return value
    # 尝试匹配常见前缀
    m = _API_KEY_PATTERN.match(value)
    if m:
        prefix = m.group(1)
        middle = m.group(2)
        suffix = m.group(3)
        return f"{prefix}{middle[:4]}...{suffix[-4:]}"
    # 通用脱敏: 前4后4
    return f"{value[:4]}...{value[-4:]}"

# A-P1-30：以下 webhook 提供方把【机器人 token 嵌在 URL 路径/查询里】，
# 该 token 本身即凭据——webhook_url 必须脱敏，不能明文出现在 GET /config 等响应中。
_WEBHOOK_HOST_HINTS = (
    "open.feishu.cn", "open.larksuite.com",          # 飞书/Lark
    "oapi.dingtalk.com",                             # 钉钉
    "qyapi.weixin.qq.com",                           # 企业微信
    "hooks.slack.com", "discord.com/api/webhooks",   # Slack / Discord
    "discordapp.com/api/webhooks",
)


def _is_webhook_url(key_lower: str, value: str) -> bool:
    """key 名为 webhook_url，或 *_url 且 host 命中已知 webhook 提供方 → 视为含凭据需脱敏。"""
    if "webhook_url" in key_lower:
        return True
    if key_lower.endswith("_url") or key_lower == "url":
        low = value.lower()
        return any(h in low for h in _WEBHOOK_HOST_HINTS)
    return False


def _mask_webhook_url(value: str) -> str:
    """脱敏 webhook URL：保留协议+host 头部与尾部少量字符，隐去含 token 的中段。"""
    if not value or len(value) <= 24:
        # 太短：整体隐去中段，避免短 token 全暴露
        return _mask_api_key(value) if value and len(value) >= 12 else value
    return f"{value[:20]}…{value[-6:]}"


from urllib.parse import urlsplit, urlunsplit


def _has_uri_credentials(v: str) -> bool:
    try:
        return bool(urlsplit(v).password)
    except ValueError:
        return False


def _mask_uri_credentials(v: str) -> str:
    """把 URI 里 user:password@ 的 password 段脱敏为 ***（对抗复核：postgres_uri/redis_uri
    默认含 swarm:swarm，_mask_config_dict 原只掩键名漏了 *_uri 里的密码）。

    用 urlsplit 正确解析（不用正则）——覆盖 Redis 空用户名 redis://:pass@ 且不被密码里的 @
    截断（正则版会漏掩 p@ss 的 @ss 后缀，对抗复核 Finding 5）。无密码段则原样返回。
    """
    try:
        parts = urlsplit(v)
    except ValueError:
        return v
    if not parts.password:
        return v
    userinfo = ("%s:***" % parts.username) if parts.username else ":***"
    # 重组 netloc：userinfo@host[:port]
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    netloc = f"{userinfo}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _mask_config_dict(cfg: dict) -> dict:
    """递归脱敏配置中的 API Key / webhook_url / URI 内嵌凭据 字段（token 即凭据）"""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        key_lower = k.lower()
        if isinstance(v, str) and any(
            p in key_lower for p in ("api_key", "apikey", "secret", "password")
        ):
            out[k] = _mask_api_key(v)
        elif isinstance(v, str) and _has_uri_credentials(v):
            # postgres_uri/redis_uri 等：掩掉内嵌密码，保留 host/db 供运维辨识。
            out[k] = _mask_uri_credentials(v)
        elif isinstance(v, str) and _is_webhook_url(key_lower, v):
            out[k] = _mask_webhook_url(v)
        elif isinstance(v, dict):
            out[k] = _mask_config_dict(v)
        elif isinstance(v, list):
            out[k] = [
                _mask_config_dict(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            out[k] = v
    return out


# ─── 配置键解析 ─────────────────────────────────────────

def _resolve_key(key: str) -> str:
    """短名 → 完整环境变量名"""
    return _SHORT_KEY_MAP.get(key, key)

def _flatten_model_config(cfg: AppConfig) -> dict[str, Any]:
    """供前端使用的扁平模型配置"""
    m = cfg.model
    return {
        "siliconflow_api_key": m.siliconflow_api_key,
        "siliconflow_base_url": m.siliconflow_base_url,
        "local_api_key": m.local_api_key,
        "local_base_url": m.local_base_url,
        "brain_primary": m.brain_primary,
        "brain_fallback": m.brain_fallback,
        "worker_primary": m.worker_primary,
        # fallback 现为多级兜底链(list)，对 WebUI 序列化成逗号串便于单字段展示/编辑往返
        "routing_trivial": m.routing_trivial,
        "routing_trivial_fallback": ",".join(m.routing_trivial_fallback),
        "routing_medium": m.routing_medium,
        "routing_medium_fallback": ",".join(m.routing_medium_fallback),
        "routing_complex": m.routing_complex,
        "routing_complex_fallback": ",".join(m.routing_complex_fallback),
        "routing_multimodal": m.routing_multimodal,
        "routing_multimodal_fallback": ",".join(m.routing_multimodal_fallback),
        "tier_enabled": m.tier_enabled,
        "tier": m.tier,
    }


# ─── 鉴权 ─────────────────────────────────────────

def _require_user(request: Request):
    from swarm.api.deps import get_current_user

    return get_current_user(request)

def _require_perm(request: Request, permission: str, project_id: str | None = None):
    from swarm.auth.store import user_can_on_project

    user = _require_user(request)
    # H6: 默认弱密码硬门槛 —— must_change_password=True 时只放行 auth/password 相关权限，
    # 其余操作一律 423 Locked，防止未改密用户越权操作。
    # 改密码端点(auth_change_password)使用 _require_user 而非 _require_perm，不会死锁。
    if getattr(user, "must_change_password", False):
        perm_prefix = permission.split(":")[0]
        if perm_prefix not in ("auth", "password"):
            raise HTTPException(
                status_code=423,
                detail="Password change required before proceeding",
            )
    if not user_can_on_project(user, permission, project_id):
        raise HTTPException(status_code=403, detail=f"Permission denied: {permission}")
    return user

def _profile_storage_key(user_id: str, project_id: str) -> str:
    from swarm.auth.store import profile_key

    return profile_key(user_id, project_id)


# ─── 查询参数 ─────────────────────────────────────────

# ─── 跨域共享 Pydantic 模型 ───────────────────────
class ApplyDiffRequest(BaseModel):
    """将 merged_diff 应用到项目工作区"""
    diff: str | None = Field(default=None, description="可选覆盖 task.merged_diff")
    check_only: bool = Field(default=False, description="仅 git apply --check")
