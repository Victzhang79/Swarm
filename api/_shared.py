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

_EMBEDDING_ZERO = [0.0] * 1024  # 零向量占位，维度与 bge-m3 一致


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

def _mask_config_dict(cfg: dict) -> dict:
    """递归脱敏配置中的 API Key 字段"""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        key_lower = k.lower()
        if isinstance(v, str) and any(
            p in key_lower for p in ("api_key", "apikey", "secret", "password")
        ):
            out[k] = _mask_api_key(v)
        elif isinstance(v, dict):
            out[k] = _mask_config_dict(v)
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
        "routing_trivial": m.routing_trivial,
        "routing_trivial_fallback": m.routing_trivial_fallback,
        "routing_medium": m.routing_medium,
        "routing_medium_fallback": m.routing_medium_fallback,
        "routing_complex": m.routing_complex,
        "routing_complex_fallback": m.routing_complex_fallback,
        "routing_multimodal": m.routing_multimodal,
        "routing_multimodal_fallback": m.routing_multimodal_fallback,
    }


# ─── 鉴权 ─────────────────────────────────────────

def _require_user(request: Request):
    from swarm.api.deps import get_current_user

    return get_current_user(request)

def _require_perm(request: Request, permission: str, project_id: str | None = None):
    from swarm.auth.store import user_can_on_project

    user = _require_user(request)
    if not user_can_on_project(user, permission, project_id):
        raise HTTPException(status_code=403, detail=f"Permission denied: {permission}")
    return user

def _profile_storage_key(user_id: str, project_id: str) -> str:
    from swarm.auth.store import profile_key

    return profile_key(user_id, project_id)


# ─── 查询参数 ─────────────────────────────────────────

def _parse_since_param(since: str | None) -> datetime | None:
    """解析 ?since= ISO8601 时间戳"""
    if not since:
        return None
    from datetime import datetime

    text = since.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid since timestamp: {since}") from exc


# ─── 跨域共享 Pydantic 模型 ───────────────────────
class ApplyDiffRequest(BaseModel):
    """将 merged_diff 应用到项目工作区"""
    diff: str | None = Field(default=None, description="可选覆盖 task.merged_diff")
    check_only: bool = Field(default=False, description="仅 git apply --check")
