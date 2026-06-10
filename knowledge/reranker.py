"""Cross-encoder reranker — SiliconFlow / OpenAI-compatible rerank API。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from swarm.config.settings import get_config

logger = logging.getLogger(__name__)


def rerank_documents(
    query: str,
    documents: list[dict[str, Any]],
    *,
    top_k: int = 5,
    text_key: str = "content",
) -> list[dict[str, Any]]:
    """对候选 chunk 重排；失败时按原 score 降序截断。"""
    if not documents or not query.strip():
        return documents[:top_k]

    cfg = get_config()
    api_key = cfg.model.siliconflow_api_key or ""
    base_url = (cfg.model.siliconflow_base_url or "").rstrip("/")
    model = cfg.knowledge.reranker_model

    if not api_key or not base_url:
        return _fallback_sort(documents, top_k)

    texts = []
    for doc in documents:
        t = doc.get(text_key) or doc.get("text") or doc.get("chunk") or ""
        if not t and doc.get("file_path"):
            t = f"{doc.get('file_path')} {doc.get('symbol_name', '')}"
        texts.append(str(t)[:2000])

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{base_url}/rerank",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "query": query,
                    "documents": texts,
                    "top_n": min(top_k, len(texts)),
                },
            )
            if resp.status_code == 404:
                return _rerank_via_embeddings_fallback(query, documents, top_k, client, base_url, api_key)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or data.get("data") or []
            out: list[dict[str, Any]] = []
            for item in results:
                idx = item.get("index", item.get("document", {}).get("index"))
                if idx is None:
                    continue
                doc = dict(documents[int(idx)])
                doc["rerank_score"] = item.get("relevance_score", item.get("score", 0.0))
                out.append(doc)
            if out:
                return out[:top_k]
    except Exception as exc:
        logger.warning("rerank API failed, using score fallback: %s", exc)

    return _fallback_sort(documents, top_k)


def _fallback_sort(documents: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    ranked = sorted(documents, key=lambda x: x.get("rerank_score", x.get("score", 0.0)), reverse=True)
    return ranked[:top_k]


def _rerank_via_embeddings_fallback(
    query: str,
    documents: list[dict[str, Any]],
    top_k: int,
    client: httpx.Client,
    base_url: str,
    api_key: str,
) -> list[dict[str, Any]]:
    """部分提供商无 /rerank，用 embeddings cosine 近似。"""
    try:
        resp = client.post(
            f"{base_url}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": get_config().knowledge.embedding_model, "input": [query] + [
                (d.get("content") or d.get("text") or "")[:512] for d in documents
            ]},
        )
        resp.raise_for_status()
        embs = [e["embedding"] for e in resp.json().get("data", [])]
        if len(embs) < 2:
            return _fallback_sort(documents, top_k)
        q = embs[0]

        def cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(x * x for x in b) ** 0.5
            return dot / (na * nb + 1e-9)

        scored = []
        for i, doc in enumerate(documents):
            doc = dict(doc)
            doc["rerank_score"] = cosine(q, embs[i + 1])
            scored.append(doc)
        scored.sort(key=lambda x: x["rerank_score"], reverse=True)
        return scored[:top_k]
    except Exception as exc:
        logger.warning("embedding rerank fallback failed: %s", exc)
        return _fallback_sort(documents, top_k)
