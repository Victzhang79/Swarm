"""Embed / Rerank 接入点配置解析 — 统一真相源。

封装「读取有效 embed/rerank 配置」逻辑，供 embed_client / reranker / 配置端点共用：
- key 优先从 secret_store 取（kb_embed_api_key / kb_rerank_api_key），回退 KnowledgeConfig 明文字段（向后兼容 .env）。
- 复用 LLM provider key：embed/rerank 自己无 key + 配了 reuse_provider 时，从该 provider 取 key，
  但仅在 **base_url 同源**（同一 host）时才允许，避免把 A 家 key 发给 B 家端点（防 key 错配）。
- catalog 预置：embed/rerank 各自的预置云端选项（开箱选了自动填 base_url/format）。

设计见 docs/Embed_Rerank_Config_Design.md（方案 A）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# secret_store key 名（敏感 key 加密存储）
SECRET_EMBED_KEY = "kb_embed_api_key"
SECRET_RERANK_KEY = "kb_rerank_api_key"


# ─── 预置 catalog（开箱即用，选了自动填 base_url/format）──────────────────
EMBED_CATALOG = [
    {"id": "siliconflow", "label": "SiliconFlow（bge-m3）", "base_url": "https://api.siliconflow.cn/v1",
     "model": "BAAI/bge-m3", "format": "openai"},
    {"id": "openai", "label": "OpenAI（text-embedding-3）", "base_url": "https://api.openai.com/v1",
     "model": "text-embedding-3-small", "format": "openai"},
    {"id": "zhipu", "label": "智谱 GLM（embedding）", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "model": "embedding-3", "format": "openai"},
    {"id": "self_hosted", "label": "自建服务（OpenAI 兼容 /embeddings）", "base_url": "",
     "model": "BAAI/bge-m3", "format": "openai"},
]

RERANK_CATALOG = [
    {"id": "self_hosted", "label": "自建服务（{query,texts}→[{index,score}]）", "base_url": "",
     "model": "BAAI/bge-reranker-v2-m3", "format": "simple"},
    {"id": "siliconflow", "label": "SiliconFlow rerank", "base_url": "https://api.siliconflow.cn/v1",
     "model": "BAAI/bge-reranker-v2-m3", "format": "openai_rerank"},
    {"id": "cohere", "label": "Cohere rerank", "base_url": "https://api.cohere.ai/v1",
     "model": "rerank-multilingual-v3.0", "format": "cohere_rerank"},
]


@dataclass
class EmbedEndpoint:
    base_url: str
    api_key: str
    model: str
    fmt: str
    batch_size: int


@dataclass
class RerankEndpoint:
    url: str           # rerank 用整 url（simple）或 base_url（openai_rerank/cohere）
    api_key: str
    model: str
    fmt: str


def _same_origin(url_a: str, url_b: str) -> bool:
    """两个 url 是否同源（scheme+host+port 相同）。复用 key 的安全闸门。"""
    try:
        pa, pb = urlparse(url_a), urlparse(url_b)
        return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)
    except Exception:  # noqa: BLE001
        return False


def _provider_key_if_same_origin(provider_id: str, target_url: str) -> str:
    """从 LLM provider 取 key，仅当 provider.base_url 与 target_url 同源时返回，否则空。

    三道防线之「同源校验」+「不静默」：异源/缺失都记日志返回空。
    """
    if not provider_id or not target_url:
        return ""
    try:
        from swarm.config.settings import ModelConfig
        for p in (ModelConfig()._effective_providers() or []):
            if getattr(p, "id", "") == provider_id:
                pbase = getattr(p, "base_url", "") or ""
                pkey = getattr(p, "api_key", "") or ""
                if not pkey:
                    logger.warning("复用 provider key 失败：provider=%s 无 key", provider_id)
                    return ""
                if not _same_origin(pbase, target_url):
                    logger.warning(
                        "复用 provider key 被拒（异源）：provider=%s base=%s 与目标=%s 不同源",
                        provider_id, pbase, target_url,
                    )
                    return ""
                return pkey
        logger.warning("复用 provider key 失败：provider=%s 不存在", provider_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("复用 provider key 解析异常: %s", exc)
    return ""


def _resolve_key(own_key_field: str, secret_name: str, reuse_provider: str, target_url: str) -> str:
    """key 解析优先级：自己的明文字段 → secret_store → 复用 provider（同源）。

    自己有 key 优先（向后兼容 .env）；否则查 secret_store；再否则复用 provider key（同源校验）。
    """
    if own_key_field:
        return own_key_field
    # secret_store
    try:
        from swarm.config import secret_store
        sk = secret_store.get_secret(secret_name)
        if sk:
            return sk
    except Exception as exc:  # noqa: BLE001
        logger.debug("secret_store 读取 %s 失败: %s", secret_name, exc)
    # 复用 provider（同源）
    if reuse_provider:
        return _provider_key_if_same_origin(reuse_provider, target_url)
    return ""


def get_embed_endpoint() -> EmbedEndpoint | None:
    """解析有效 embedding 接入点。base_url 为空返回 None（调用方回退旧链）。"""
    try:
        from swarm.config.settings import KnowledgeConfig
        k = KnowledgeConfig()
    except Exception:  # noqa: BLE001
        return None
    base = (k.embed_base_url or "").strip().rstrip("/")
    if not base:
        return None
    key = _resolve_key(k.embed_api_key or "", SECRET_EMBED_KEY, k.embed_reuse_provider or "", base)
    return EmbedEndpoint(
        base_url=base, api_key=key, model=k.embedding_model,
        fmt=(k.embed_format or "openai"), batch_size=int(getattr(k, "embed_batch_size", 32) or 32),
    )


def get_rerank_endpoint() -> RerankEndpoint | None:
    """解析有效 rerank 接入点。url/base 为空返回 None（调用方回退）。"""
    try:
        from swarm.config.settings import KnowledgeConfig
        k = KnowledgeConfig()
    except Exception:  # noqa: BLE001
        return None
    url = (k.rerank_url or "").strip().rstrip("/")
    if not url:
        return None
    key = _resolve_key(k.rerank_api_key or "", SECRET_RERANK_KEY, k.rerank_reuse_provider or "", url)
    return RerankEndpoint(
        url=url, api_key=key, model=k.reranker_model, fmt=(k.rerank_format or "simple"),
    )
