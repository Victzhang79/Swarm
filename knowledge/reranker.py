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
    kcfg = cfg.knowledge

    texts = []
    for doc in documents:
        t = doc.get(text_key) or doc.get("text") or doc.get("chunk") or ""
        if not t and doc.get("file_path"):
            t = f"{doc.get('file_path')} {doc.get('symbol_name', '')}"
        texts.append(str(t)[:2000])

    # 批2 改造：从统一解析取 rerank 接入点（含 secret_store key / 复用 provider key 同源校验），
    # 按 rerank_format 选适配器（simple / openai_rerank / cohere_rerank）。
    ep = None
    try:
        from swarm.knowledge.embed_rerank_config import get_rerank_endpoint
        ep = get_rerank_endpoint()
    except Exception as exc:  # noqa: BLE001
        logger.debug("rerank 接入点解析失败: %s", exc)

    if ep is not None:
        thr = getattr(kcfg, "rerank_score_threshold", 0.0) or 0.0
        try:
            if ep.fmt == "simple":
                out = _rerank_simple(ep, query, texts, documents)
            elif ep.fmt == "cohere_rerank":
                out = _rerank_cohere(ep, query, texts, documents, top_k)
            else:  # openai_rerank（含 SiliconFlow）
                out = _rerank_openai(ep, query, texts, documents, top_k)
            if out:
                out.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
                if thr > 0:
                    out = [d for d in out if d.get("rerank_score", 0.0) >= thr] or out[:1]
                return out[:top_k]
        except Exception as exc:  # noqa: BLE001
            logger.warning("rerank(%s) 失败(回退本地排序): %s", ep.fmt, exc)

    return _fallback_sort(documents, top_k)


def _record_rerank_usage(ep, query: str, texts: list[str]) -> None:
    """B3：rerank 记账（best-effort）。rerank API 一般不回 usage，用 len//4 估算 query+docs。"""
    try:
        from swarm.models import usage_tracker
        pt = (len(query or "") + sum(len(t or "") for t in texts)) // 4
        if pt > 0:
            usage_tracker.record_embed(
                getattr(ep, "model", "") or "rerank", getattr(ep, "url", ""), pt, op="rerank")
    except Exception:  # noqa: BLE001
        pass



# ── D54：进程级共享 httpx.Client（线程安全，连接池复用）────────────────────
# 旧行为：每次 rerank 每种格式函数都 `with httpx.Client(...)` 新建再关闭（每请求新建
# 连接/TLS）。改为模块级单例 + nullcontext 包装（不随 with 关闭）；构造失败回退旧的
# 一次性 client（fail-closed 不丢功能）。
_SHARED_CLIENT: "httpx.Client | None" = None
_SHARED_CLIENT_LOCK = None
# 模块导入时捕获真实类：单测 monkeypatch reranker.httpx.Client 注入假 client 时，
# 走一次性构造（等价旧行为），mock 可注入且不污染共享单例。
_REAL_HTTPX_CLIENT_CLS = httpx.Client


def _client_cm():
    """返回可用于 `with` 的 client 上下文：共享单例（nullcontext）或一次性回退。"""
    import contextlib
    global _SHARED_CLIENT, _SHARED_CLIENT_LOCK
    if httpx.Client is not _REAL_HTTPX_CLIENT_CLS:
        return httpx.Client(timeout=30.0)  # 测试 seam / 运行时替换：按旧行为逐次构造
    try:
        if _SHARED_CLIENT is None:
            import threading
            if _SHARED_CLIENT_LOCK is None:
                _SHARED_CLIENT_LOCK = threading.Lock()
            with _SHARED_CLIENT_LOCK:
                if _SHARED_CLIENT is None:
                    _SHARED_CLIENT = httpx.Client(timeout=30.0)
        return contextlib.nullcontext(_SHARED_CLIENT)
    except Exception:  # noqa: BLE001
        return httpx.Client(timeout=30.0)


def _rerank_simple(ep, query: str, texts: list[str], documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """自建格式：POST {query, texts} → [{index, score}] 或 {results:[...]}。"""
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    with _client_cm() as client:  # D54：共享连接池，失败回退一次性 client
        resp = client.post(ep.url, json={"query": query, "texts": texts}, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    _record_rerank_usage(ep, query, texts)
    items = data if isinstance(data, list) else (data.get("results") or data.get("data") or [])
    out: list[dict[str, Any]] = []
    for item in items:
        idx = item.get("index")
        if idx is None or int(idx) >= len(documents):
            continue
        doc = dict(documents[int(idx)])
        doc["rerank_score"] = item.get("score", item.get("relevance_score", 0.0))
        out.append(doc)
    return out


def _rerank_openai(ep, query: str, texts: list[str], documents: list[dict[str, Any]],
                   top_k: int) -> list[dict[str, Any]]:
    """SiliconFlow/OpenAI 兼容：POST {base}/rerank {model,query,documents,top_n}。"""
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    base = ep.url.rstrip("/")
    url = base if base.endswith("/rerank") else f"{base}/rerank"
    with _client_cm() as client:  # D54：共享连接池，失败回退一次性 client
        resp = client.post(url, headers=headers, json={
            "model": ep.model, "query": query, "documents": texts,
            "top_n": min(top_k, len(texts)),
        })
        if resp.status_code == 404:
            return _rerank_via_embeddings_fallback(query, documents, top_k, client, base, ep.api_key)
        resp.raise_for_status()
        data = resp.json()
    _record_rerank_usage(ep, query, texts)
    results = data.get("results") or data.get("data") or []
    out: list[dict[str, Any]] = []
    for item in results:
        idx = item.get("index", item.get("document", {}).get("index"))
        if idx is None or int(idx) >= len(documents):
            continue
        doc = dict(documents[int(idx)])
        doc["rerank_score"] = item.get("relevance_score", item.get("score", 0.0))
        out.append(doc)
    return out


def _rerank_cohere(ep, query: str, texts: list[str], documents: list[dict[str, Any]],
                   top_k: int) -> list[dict[str, Any]]:
    """Cohere /v1/rerank：POST {base}/rerank {model,query,documents,top_n} → {results:[{index,relevance_score}]}。"""
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    base = ep.url.rstrip("/")
    url = base if base.endswith("/rerank") else f"{base}/rerank"
    with _client_cm() as client:  # D54：共享连接池，失败回退一次性 client
        resp = client.post(url, headers=headers, json={
            "model": ep.model, "query": query, "documents": texts,
            "top_n": min(top_k, len(texts)),
        })
        resp.raise_for_status()
        data = resp.json()
    _record_rerank_usage(ep, query, texts)
    results = data.get("results") or []
    out: list[dict[str, Any]] = []
    for item in results:
        idx = item.get("index")
        if idx is None or int(idx) >= len(documents):
            continue
        doc = dict(documents[int(idx)])
        doc["rerank_score"] = item.get("relevance_score", 0.0)
        out.append(doc)
    return out


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
