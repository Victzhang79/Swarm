"""统一 embedding 客户端 — 调专用 embed 服务(SWARM_KB_EMBED_BASE_URL, bge-m3)。

避免在 preprocess / SemanticIndexer / MemoryStore 各写一份。提供同步与异步两个入口。
都不可用时由各调用方决定回退（零向量并告警）。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# 服务端 batch 上限（ai.bit:8082 bge-m3 = 32）。超过需分批，否则 422。
_MAX_BATCH = 32

# F10c：批级重试次数。契约 len(out)==len(texts) 要求全对齐，故任一批永久失败仍须整次放弃(return
# None)；但【transient 抖动】(网络瞬断/服务 5xx)不该让已成功的前若干批全部作废重算——先就地重试。
_BATCH_RETRIES = 3


def _batch_backoff_sleep(attempt: int) -> None:
    time.sleep(0.5 * (attempt + 1))


# ── D54：HTTP 客户端复用 ──────────────────────────────────────────────
# 旧行为：sync 路径每次 requests.post 裸调（每请求新建连接/TLS）、async 路径每次
# embed_texts_async 新建 AsyncClient。改为：sync 用进程级 requests.Session（线程安全，
# 连接池复用）；async 用【按事件循环】缓存的 AsyncClient（httpx AsyncClient 的连接池
# 绑定创建它的 loop，跨 loop 复用会炸——测试/脚本多次 asyncio.run 是常态，故按 loop 键控，
# loop 已关闭的条目惰性清理）。任一环节异常 → 回退一次性客户端（fail-closed 不丢功能）。
_SYNC_SESSION = None
_SYNC_SESSION_LOCK = None


def _sync_session():
    """进程级 requests.Session（惰性单例）；构造失败返回 None，调用方回退裸 requests。"""
    global _SYNC_SESSION, _SYNC_SESSION_LOCK
    if _SYNC_SESSION is not None:
        return _SYNC_SESSION
    try:
        import threading
        if _SYNC_SESSION_LOCK is None:
            _SYNC_SESSION_LOCK = threading.Lock()
        import requests
        with _SYNC_SESSION_LOCK:
            if _SYNC_SESSION is None:
                _SYNC_SESSION = requests.Session()
        return _SYNC_SESSION
    except Exception:  # noqa: BLE001
        return None


_ASYNC_CLIENTS: dict = {}  # id(loop) -> (loop, httpx.AsyncClient)


def _async_client():
    """当前事件循环的共享 AsyncClient；获取失败返回 None（调用方回退一次性 client）。"""
    try:
        import asyncio

        import httpx
        loop = asyncio.get_running_loop()
        # 惰性清理已关闭 loop 的条目（防长期进程里 loop 轮换累积）
        dead = [k for k, (lp, _c) in _ASYNC_CLIENTS.items() if lp.is_closed()]
        for k in dead:
            _ASYNC_CLIENTS.pop(k, None)
        entry = _ASYNC_CLIENTS.get(id(loop))
        if entry is not None and entry[0] is loop:
            return entry[1]
        client = httpx.AsyncClient(timeout=60.0)
        _ASYNC_CLIENTS[id(loop)] = (loop, client)
        return client
    except Exception:  # noqa: BLE001
        return None


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


def _record_embed_usage(model: str, base: str, batch: list[str], usage: dict | None) -> None:
    """B3：embed 一批的 token 记账——优先响应真实 usage.prompt_tokens，否则 len//4 估算。

    ★复核 SF-1 修正：整体 try/except——本函数在 embed_texts_* 的外层 try 内被调用，若这里抛
    (如 usage.prompt_tokens 为 float 触发 int() ValueError)，异常会被外层 `except: return None`
    捕获 → 丢弃【已成功计算的 embeddings】致检索静默降级。记账失败绝不能拖垮 embed 主结果。
    """
    try:
        pt = int(float((usage or {}).get("prompt_tokens") or 0)) or (sum(len(t or "") for t in batch) // 4)
        if pt <= 0:
            return
        from swarm.models import usage_tracker
        usage_tracker.record_embed(model, base, pt, op="embed")
    except Exception:  # noqa: BLE001 — 记账 best-effort，永不影响 embed 主结果
        pass


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
            vecs = None
            last_err = ""
            for attempt in range(_BATCH_RETRIES):
                try:
                    # D54：优先复用进程级 Session（连接池），失败回退裸 requests（行为不变）。
                    # requests.post 被 patch（单测 seam）时也走裸调，保 mock 可注入。
                    _sess = _sync_session()
                    _patched = requests.post is not getattr(
                        getattr(requests, "api", None), "post", requests.post)
                    _poster = _sess.post if (_sess is not None and not _patched) else requests.post
                    resp = _poster(
                        f"{base}/embeddings",
                        json={"model": model, "input": batch},
                        headers=headers,
                        timeout=120,
                    )
                    if resp.status_code != 200:
                        last_err = f"status={resp.status_code}"
                    else:
                        _payload = resp.json()
                        cand = [d["embedding"] for d in _payload.get("data", [])]
                        if len(cand) == len(batch):
                            vecs = cand
                            _record_embed_usage(model, base, batch, _payload.get("usage"))
                            break
                        last_err = f"数量不符 {len(cand)}!={len(batch)}"
                except Exception as exc:  # noqa: BLE001 — 批内瞬时错误，进入重试
                    last_err = str(exc)
                if attempt < _BATCH_RETRIES - 1:
                    _batch_backoff_sleep(attempt)
            if vecs is None:
                # F10c：批级重试耗尽才放弃整次（契约 len==len 需全对齐）；transient 抖动不再丢弃已成功批。
                logger.warning("embed 批 %d 重试 %d 次仍失败，放弃整次: %s",
                               i // max_batch, _BATCH_RETRIES, last_err)
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
        import asyncio
        import contextlib
        out: list[list[float]] = []
        # D54：复用本 loop 的共享 AsyncClient（nullcontext 包装=不随 with 关闭）；
        # 获取失败回退旧的一次性 client（fail-closed）。
        _shared = _async_client()
        _client_cm = (contextlib.nullcontext(_shared) if _shared is not None
                      else httpx.AsyncClient(timeout=60.0))
        async with _client_cm as client:
            for i in range(0, len(texts), max_batch):
                batch = texts[i: i + max_batch]
                vecs = None
                last_err = ""
                for attempt in range(_BATCH_RETRIES):
                    try:
                        resp = await client.post(
                            f"{base}/embeddings",
                            json={"model": model, "input": batch},
                            headers=headers,
                        )
                        resp.raise_for_status()
                        _payload = resp.json()
                        cand = [d["embedding"] for d in _payload.get("data", [])]
                        if len(cand) == len(batch):
                            vecs = cand
                            _record_embed_usage(model, base, batch, _payload.get("usage"))
                            break
                        last_err = f"数量不符 {len(cand)}!={len(batch)}"
                    except Exception as exc:  # noqa: BLE001 — 批内瞬时错误，进入重试
                        last_err = str(exc)
                    if attempt < _BATCH_RETRIES - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
                if vecs is None:
                    # F10c：批级重试耗尽才放弃整次；transient 抖动不再丢弃已成功批。
                    logger.warning("embed(async) 批 %d 重试 %d 次仍失败，放弃整次: %s",
                                   i // max_batch, _BATCH_RETRIES, last_err)
                    return None
                out.extend(vecs)
        return out if out else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("embed 服务(async)调用失败: %s", exc)
    return None
