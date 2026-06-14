"""统一 embedding 客户端 — 调专用 embed 服务(SWARM_KB_EMBED_BASE_URL, bge-m3)。

避免在 preprocess / SemanticIndexer / MemoryStore 各写一份。提供同步与异步两个入口。
都不可用时由各调用方决定回退（零向量并告警）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 服务端 batch 上限（ai.bit:8082 bge-m3 = 32）。超过需分批，否则 422。
_MAX_BATCH = 32


def _endpoint() -> tuple[str, str, str, int] | None:
    """返回 (base_url, api_key, model, batch_size) 或 None（未配置）。

    委托 embed_rerank_config.get_embed_endpoint（统一解析：secret_store key +
    复用 provider key 同源校验）。批2 改造：不再直接读 KnowledgeConfig 明文字段。
    """
    try:
        from swarm.knowledge.embed_rerank_config import get_embed_endpoint
        ep = get_embed_endpoint()
        if ep is None:
            return None
        return ep.base_url, ep.api_key, ep.model, int(ep.batch_size or 32)
    except Exception:  # noqa: BLE001
        return None


def embed_texts_sync(texts: list[str]) -> list[list[float]] | None:
    """同步嵌入；专用服务不可用返回 None（调用方回退）。自动按 _MAX_BATCH 分批。"""
    ep = _endpoint()
    if not ep:
        return None
    base, api_key, model, max_batch = ep
    try:
        import requests
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        out: list[list[float]] = []
        for i in range(0, len(texts), max_batch):
            batch = texts[i: i + max_batch]
            resp = requests.post(
                f"{base}/embeddings",
                json={"model": model, "input": batch},
                headers=headers,
                timeout=120,
            )
            if resp.status_code != 200:
                logger.warning("embed 服务返回异常 status=%s (batch %d)", resp.status_code, i // _MAX_BATCH)
                return None
            vecs = [d["embedding"] for d in resp.json().get("data", [])]
            if len(vecs) != len(batch):
                logger.warning("embed 返回数量不符: %d != %d", len(vecs), len(batch))
                return None
            out.extend(vecs)
        return out if out else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed 服务(sync)调用失败: %s", exc)
    return None


async def embed_texts_async(texts: list[str]) -> list[list[float]] | None:
    """异步嵌入；专用服务不可用返回 None（调用方回退）。自动按 _MAX_BATCH 分批。"""
    ep = _endpoint()
    if not ep:
        return None
    base, api_key, model, max_batch = ep
    try:
        import httpx
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60.0) as client:
            for i in range(0, len(texts), max_batch):
                batch = texts[i: i + max_batch]
                resp = await client.post(
                    f"{base}/embeddings",
                    json={"model": model, "input": batch},
                    headers=headers,
                )
                resp.raise_for_status()
                vecs = [d["embedding"] for d in resp.json().get("data", [])]
                if len(vecs) != len(batch):
                    return None
                out.extend(vecs)
        return out if out else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed 服务(async)调用失败: %s", exc)
    return None
