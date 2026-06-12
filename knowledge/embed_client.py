"""统一 embedding 客户端 — 调专用 embed 服务(SWARM_KB_EMBED_BASE_URL, bge-m3)。

避免在 preprocess / SemanticIndexer / MemoryStore 各写一份。提供同步与异步两个入口。
都不可用时由各调用方决定回退（零向量并告警）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _endpoint() -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model) 或 None（未配置）。"""
    try:
        from swarm.config.settings import KnowledgeConfig
        kcfg = KnowledgeConfig()
        base = (kcfg.embed_base_url or "").strip().rstrip("/")
        if not base:
            return None
        return base, (kcfg.embed_api_key or ""), kcfg.embedding_model
    except Exception:  # noqa: BLE001
        return None


def embed_texts_sync(texts: list[str]) -> list[list[float]] | None:
    """同步嵌入；专用服务不可用返回 None（调用方回退）。"""
    ep = _endpoint()
    if not ep:
        return None
    base, api_key, model = ep
    try:
        import requests
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.post(
            f"{base}/embeddings",
            json={"model": model, "input": texts},
            headers=headers,
            timeout=120,
        )
        if resp.status_code == 200:
            vecs = [d["embedding"] for d in resp.json().get("data", [])]
            if vecs and len(vecs) == len(texts):
                return vecs
        logger.warning("embed 服务返回异常 status=%s", resp.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed 服务(sync)调用失败: %s", exc)
    return None


async def embed_texts_async(texts: list[str]) -> list[list[float]] | None:
    """异步嵌入；专用服务不可用返回 None（调用方回退）。"""
    ep = _endpoint()
    if not ep:
        return None
    base, api_key, model = ep
    try:
        import httpx
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base}/embeddings",
                json={"model": model, "input": texts},
                headers=headers,
            )
            resp.raise_for_status()
            vecs = [d["embedding"] for d in resp.json().get("data", [])]
            if vecs and len(vecs) == len(texts):
                return vecs
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed 服务(async)调用失败: %s", exc)
    return None
