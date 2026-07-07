"""P2-14 D54 / D55 行为与机制测试。

D54：LLM/HTTP 客户端复用——同参数取到同一实例、参数变化（含热更后的 base_url/api_key 变化）
     取到新实例、外部回调 fail-closed 不缓存；embed/rerank 客户端进程级复用且跨 loop 不错共享。
D55：GitLab MR 同步的同步 HTTP 卸线程——调用期间事件循环不被冻结。
"""

from __future__ import annotations

import asyncio
import time

from swarm.config.settings import ModelConfig, ProviderConfig


def _provider(base_url="http://llm.local/v1", api_key="k1", kind="local"):
    from swarm.models.router import EndpointProvider

    return EndpointProvider(
        ProviderConfig(id="p1", label="P1", kind=kind, base_url=base_url, api_key=api_key),
        ModelConfig(),
    )


# ─── D54a：ChatModel 实例缓存 ───────────────────────────


def _fresh_cache():
    from swarm.models import router

    router.clear_chat_model_cache()


def test_d54_same_params_same_instance():
    _fresh_cache()
    prov = _provider()
    m1 = prov.get_chat_model("m-a", temperature=0.2)
    m2 = prov.get_chat_model("m-a", temperature=0.2)
    assert m1 is m2  # 不再每次调用重建 httpx 连接池


def test_d54_param_change_new_instance():
    _fresh_cache()
    prov = _provider()
    base = prov.get_chat_model("m-a", temperature=0.2)
    assert prov.get_chat_model("m-a", temperature=0.7) is not base      # 温度影响行为
    assert prov.get_chat_model("m-b", temperature=0.2) is not base      # 模型名
    assert prov.get_chat_model("m-a", temperature=0.2, max_tokens=64) is not base


def test_d54_config_value_change_invalidates():
    """热更语义：base_url / api_key 变化（PUT /api/routing 后 provider 值变）→ 新实例。"""
    _fresh_cache()
    m1 = _provider(base_url="http://old.local/v1").get_chat_model("m-a")
    m2 = _provider(base_url="http://new.local/v1").get_chat_model("m-a")
    assert m1 is not m2
    m3 = _provider(api_key="rotated").get_chat_model("m-a")
    assert m3 is not m1


def test_d54_known_callbacks_cached_unknown_not():
    from langchain_core.callbacks import BaseCallbackHandler

    from swarm.models.router import ModelInvocationLogger

    _fresh_cache()
    prov = _provider()
    cb = lambda: ModelInvocationLogger("worker/medium", "m-a", "p1")  # noqa: E731
    m1 = prov.get_chat_model("m-a", callbacks=[cb()])
    m2 = prov.get_chat_model("m-a", callbacks=[cb()])
    assert m1 is m2  # 仓内回调按构造参数指纹化 → 命中缓存

    class _Alien(BaseCallbackHandler):
        pass

    a1 = prov.get_chat_model("m-a", callbacks=[_Alien()])
    a2 = prov.get_chat_model("m-a", callbacks=[_Alien()])
    assert a1 is not a2  # 外部回调无法指纹化 → fail-closed 不缓存（绝不错共享）


# ─── D54b/c：embed / rerank 客户端复用 ───────────────────


def test_d54_embed_sync_session_singleton():
    from swarm.knowledge import embed_client

    s1 = embed_client._sync_session()
    s2 = embed_client._sync_session()
    assert s1 is not None and s1 is s2


def test_d54_embed_async_client_per_loop():
    from swarm.knowledge import embed_client

    got: dict = {}

    async def grab(tag):
        c1 = embed_client._async_client()
        c2 = embed_client._async_client()
        assert c1 is not None and c1 is c2  # 同 loop 复用
        got[tag] = c1

    asyncio.run(grab("loop1"))
    asyncio.run(grab("loop2"))
    # 跨 loop 绝不共享（httpx AsyncClient 连接池绑定创建 loop，错共享会 RuntimeError）
    assert got["loop1"] is not got["loop2"]


def test_d54_reranker_shared_client():
    from swarm.knowledge import reranker

    with reranker._client_cm() as c1:
        pass
    with reranker._client_cm() as c2:
        pass
    assert c1 is c2          # 进程级共享
    assert not c1.is_closed  # nullcontext 包装：with 退出不关闭共享实例


# ─── D55：MR 同步不冻结事件循环 ─────────────────────────


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _BlockingClient:
    """每次 get 同步 sleep 0.15s，模拟慢 GitLab。改前这些 sleep 直接冻结事件循环。"""

    def __init__(self, *a, **kw):
        pass

    def get(self, url, **kw):
        time.sleep(0.15)
        if url.endswith("/merge_requests"):
            return _FakeResp([{"iid": 1, "title": "a"}, {"iid": 2, "title": "b"}])
        return _FakeResp({"changes": [{"new_path": "x.py"}]})

    def close(self):
        pass


class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, sql, params):
        self._sink.append(params)


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink)

    async def close(self):
        pass


def test_d55_mr_sync_does_not_freeze_event_loop(monkeypatch):
    import types

    from swarm.knowledge import mr_history

    monkeypatch.setenv("SWARM_GITLAB_URL", "http://gitlab.local")
    monkeypatch.setenv("SWARM_GITLAB_TOKEN", "tk")
    monkeypatch.setenv("SWARM_GITLAB_PROJECT_ID", "42")
    monkeypatch.setattr(mr_history, "httpx", types.SimpleNamespace(Client=_BlockingClient))

    rows: list = []
    ticks = {"n": 0}

    async def heartbeat(stop):
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(0.01)

    async def main():
        stop = asyncio.Event()
        hb = asyncio.create_task(heartbeat(stop))
        count = await mr_history.sync_mr_history_from_gitlab(lambda: _FakeConn(rows), "proj-1")
        stop.set()
        await hb
        return count

    count = asyncio.run(main())
    assert count == 2 and len(rows) == 2      # 行为不变：2 MR 全部落库
    # 3 次 get × 0.15s ≈ 0.45s 阻塞期间心跳应持续跳动（卸线程后事件循环不冻结）。
    # 改前同步 get 在 loop 上执行，心跳只能跳个位数。
    assert ticks["n"] >= 20, ticks["n"]
