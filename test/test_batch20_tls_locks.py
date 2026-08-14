"""30 号文批20 TLS 面锁：L-2b WARNING 进程级去重 / L-2c tls_insecure 值层闸。

- L-2b：_tls_verify 的 WARNING 按 (id, hostname) 进程级去重——4 站点×每次探测
  重复报警会淹真信号；换 host 重新警（批20 R1：去重键是解析后的 hostname）。
- L-2c（拍板=拆字段）：tls_insecure false→true 视同新出站风险，非 admin 403
  （裁决先于 set_secret/persist）；前端未回传该键时保留旧值（老 UI 不能帮人关掉）。
  prober 咽喉行为矩阵在 test_prober.py::test_tls_verify_chokepoint（含隐式判据
  退役的翻转行）。
"""
from __future__ import annotations

import json
import logging
import ssl
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from swarm.api.routers import config as _cfg
from swarm.config.settings import ProviderConfig
from swarm.models import prober


# ── L-2b：WARNING 去重 ─────────────────────────────────────

def test_tls_warning_deduped_per_provider(caplog):
    """L-2b：同一 (id, hostname) 进程内只警一次。摘掉 `_wkey not in _tls_warned`
    去重闸本锁红（第二次调用会再警）。"""
    prober._reset_tls_warned()
    p = ProviderConfig(id="p-dedup", kind="local", tls_insecure=True,
                       base_url="https://api.public.example/v1", api_key="k")
    with caplog.at_level(logging.WARNING, logger="swarm.models.prober"):
        assert prober._tls_verify(p) is True
        assert prober._tls_verify(p) is True
    hits = [r for r in caplog.records if "L-2c" in r.message]
    assert len(hits) == 1, f"同 provider 同 host 重复报警（淹真信号）: {len(hits)} 次"


def test_tls_warning_rewarns_on_host_change(caplog):
    """反向钉：换 host 必须重新警（去重键含 hostname——配置变了不能闷住）。"""
    prober._reset_tls_warned()
    with caplog.at_level(logging.WARNING, logger="swarm.models.prober"):
        prober._tls_verify(ProviderConfig(id="p-dedup", kind="local", tls_insecure=True,
                                          base_url="https://a.public.example/v1", api_key="k"))
        prober._tls_verify(ProviderConfig(id="p-dedup", kind="local", tls_insecure=True,
                                          base_url="https://b.public.example/v1", api_key="k"))
    hits = [r for r in caplog.records if "L-2c" in r.message]
    assert len(hits) == 2, f"换 host 未重新警: {len(hits)} 次"


def test_tls_warning_never_leaks_userinfo(caplog):
    """★R2 hunter M2★：WARNING 文本绝不带 userinfo——base_url 里塞凭据时日志只能出现
    scheme://host。改回原始串截断（base_url[:80]）进日志，本锁红。"""
    prober._reset_tls_warned()
    with caplog.at_level(logging.WARNING, logger="swarm.models.prober"):
        prober._tls_verify(ProviderConfig(
            id="p-leak", kind="local", tls_insecure=True,
            base_url="https://admin:s3cr3t@api.public.example/v1", api_key="k"))
    hits = [r for r in caplog.records if "L-2c" in r.message]
    assert len(hits) == 1
    rendered = hits[0].getMessage()
    assert "s3cr3t" not in rendered and "admin" not in rendered, \
        f"WARNING 泄漏 userinfo 凭据: {rendered}"
    assert "api.public.example" in rendered, f"host 本身仍应可见: {rendered}"


# ── L-2c：值层闸（false→true 视同新出站风险）─────────────────

def _endpoint(path: str, method: str = "PUT"):
    for r in _cfg.router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    return None


def _harness(monkeypatch, *, admin: bool, existing: list | None = None,
             env_providers: list | None = None):
    """与批19 同形 mock 面（真闸 _reject_endpoint_keys 照跑）。"""
    user = MagicMock()
    user.username = "alice"
    user.global_role = "admin" if admin else "owner"
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: admin)

    cfg_obj = MagicMock()
    cfg_obj.model._effective_providers.return_value = existing or []
    cfg_obj.notify_channels = []
    for p in (existing or []):
        if getattr(p, "id", "") in ("siliconflow", "local"):
            setattr(cfg_obj.model, f"{p.id}_base_url", p.base_url)
    monkeypatch.setattr(_cfg._app, "get_config", lambda: cfg_obj)

    calls: dict[str, list] = {"persist": [], "set_secret": []}
    monkeypatch.setattr(
        _cfg, "_persist_env_updates",
        lambda *a, **k: calls["persist"].append((a, k)) or {"audit_status": "ok"})
    import swarm.config.secret_store as _ss
    monkeypatch.setattr(_ss, "set_secret", lambda *a, **k: calls["set_secret"].append(a))
    monkeypatch.setattr(_ss, "invalidate_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        "swarm.models.router.ModelRouter",
        lambda: MagicMock(get_routing_table=lambda: {}))
    # 值层闸旧集合（os.environ 现值）摆成与 existing 同 host，隔离本命题
    if env_providers is not None:
        monkeypatch.setenv("SWARM_MODEL_PROVIDERS", json.dumps(
            env_providers, ensure_ascii=False))
    return calls


def _req(body):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


def _persisted_providers(calls) -> list:
    assert calls["persist"], "persist 未被调用"
    um = calls["persist"][-1][0][0]
    return json.loads(um["SWARM_MODEL_PROVIDERS"])


_EXISTING = [ProviderConfig(id="local", kind="local", tls_insecure=False,
                            base_url="http://192.168.1.10:11434", api_key="")]
_ENV_SAME_HOST = [{"id": "local", "kind": "local",
                   "base_url": "http://192.168.1.10:11434", "api_key": ""}]


@pytest.mark.asyncio
async def test_tls_insecure_flip_nonadmin_403(monkeypatch):
    """L-2c 主锁：非 admin 把既有 provider 的 tls_insecure false→true ⇒ 403，
    且裁决先于 set_secret/persist（G-1 序）。删掉值层闸块本锁红。"""
    calls = _harness(monkeypatch, admin=False, existing=_EXISTING,
                     env_providers=_ENV_SAME_HOST)
    ep = _endpoint("/api/model-providers")
    with pytest.raises(HTTPException) as ei:
        await ep(_req({"providers": [{"id": "local", "kind": "local",
                                      "base_url": "http://192.168.1.10:11434",
                                      "tls_insecure": True}]}))
    assert ei.value.status_code == 403
    assert "tls_insecure" in ei.value.detail
    assert calls["persist"] == [] and calls["set_secret"] == []


@pytest.mark.asyncio
async def test_tls_insecure_flip_admin_allowed(monkeypatch):
    """反向钉：admin 开启放行且字段真落盘。"""
    calls = _harness(monkeypatch, admin=True, existing=_EXISTING,
                     env_providers=_ENV_SAME_HOST)
    ep = _endpoint("/api/model-providers")
    await ep(_req({"providers": [{"id": "local", "kind": "local",
                                  "base_url": "http://192.168.1.10:11434",
                                  "tls_insecure": True}]}))
    persisted = _persisted_providers(calls)
    assert persisted[0]["tls_insecure"] is True


@pytest.mark.asyncio
async def test_tls_insecure_true_to_true_nonadmin_ok(monkeypatch):
    """方向钉：保持 true→true 不是「开启」，非 admin 放行（过宽会逼使用者绕开）。"""
    existing_on = [ProviderConfig(id="local", kind="local", tls_insecure=True,
                                  base_url="http://192.168.1.10:11434", api_key="")]
    env_on = [dict(_ENV_SAME_HOST[0], tls_insecure=True)]
    calls = _harness(monkeypatch, admin=False, existing=existing_on,
                     env_providers=env_on)
    ep = _endpoint("/api/model-providers")
    await ep(_req({"providers": [{"id": "local", "kind": "local", "label": "改名",
                                  "base_url": "http://192.168.1.10:11434",
                                  "tls_insecure": True}]}))
    assert calls["persist"], "true→true 被误 403"


@pytest.mark.asyncio
async def test_tls_insecure_preserved_when_omitted(monkeypatch):
    """老 UI 不回传该键时必须保留旧值（同 *** key 语义）——不能帮人静默关掉。
    删掉 clean entry 的「缺省保留旧值」分支本锁红。"""
    existing_on = [ProviderConfig(id="local", kind="local", tls_insecure=True,
                                  base_url="http://192.168.1.10:11434", api_key="")]
    env_on = [dict(_ENV_SAME_HOST[0], tls_insecure=True)]
    calls = _harness(monkeypatch, admin=True, existing=existing_on,
                     env_providers=env_on)
    ep = _endpoint("/api/model-providers")
    await ep(_req({"providers": [{"id": "local", "kind": "local", "label": "改名",
                                  "base_url": "http://192.168.1.10:11434"}]}))
    persisted = _persisted_providers(calls)
    assert persisted[0].get("tls_insecure") is True, \
        f"未回传 tls_insecure 时旧 true 被静默清掉: {persisted[0]}"


# ── 批20 R1 折双复核：四条新锁 ─────────────────────────────

@pytest.mark.asyncio
async def test_tls_insecure_new_provider_true_nonadmin_403(monkeypatch):
    """★R1（reviewer M5 + hunter「rename 攻击已拦但缺锁」）★：新 id provider 直接声明
    tls_insecure=true 视同 false→true（old_by_id 查不到→缺省 False→拦），非 admin 403。
    夹具刻意用「同 host 换 id」（rename 攻击形状）：G-1b 端点闸看 host 集合差会放行，
    全靠 L-2c 闸拦——把闸里 getattr 缺省 False 改成 True，本锁红。"""
    calls = _harness(monkeypatch, admin=False, existing=_EXISTING,
                     env_providers=_ENV_SAME_HOST)
    ep = _endpoint("/api/model-providers")
    with pytest.raises(HTTPException) as ei:
        await ep(_req({"providers": [{"id": "local2", "kind": "local",
                                      "base_url": "http://192.168.1.10:11434",
                                      "tls_insecure": True}]}))
    assert ei.value.status_code == 403
    assert calls["persist"] == [] and calls["set_secret"] == []


@pytest.mark.asyncio
async def test_tls_insecure_non_bool_rejected_400(monkeypatch):
    """★R1（reviewer H1 + hunter H1）★：bool("false") is True——字符串/数字输入会被
    强转成 true 静默开洞，必须 fail-loud 400。把 isinstance 分支改回 bool() 强转，本锁红。"""
    calls = _harness(monkeypatch, admin=True, existing=_EXISTING,
                     env_providers=_ENV_SAME_HOST)
    ep = _endpoint("/api/model-providers")
    with pytest.raises(HTTPException) as ei:
        await ep(_req({"providers": [{"id": "local", "kind": "local",
                                      "base_url": "http://192.168.1.10:11434",
                                      "tls_insecure": "false"}]}))
    assert ei.value.status_code == 400
    assert calls["persist"] == [] and calls["set_secret"] == []


@pytest.mark.asyncio
async def test_list_models_tls_via_chokepoint(monkeypatch):
    """★R1（reviewer H2 + hunter M2/M6）★：GET /api/models 的 verify 必须来自
    _tls_verify 单一咽喉——私网 https + 未声明 tls_insecure ⇒ verify=True
    （旧隐式判据 `not _is_local_or_private_host(base)` 会给 False，本锁红）。
    双维度：①咽喉被调（接线）②verify 值真的流进 httpx.AsyncClient（管道）。"""
    import httpx

    user = MagicMock()
    monkeypatch.setattr(_cfg, "_require_user", lambda *a, **k: user)
    cfg_obj = MagicMock()
    p = ProviderConfig(id="local", kind="local", tls_insecure=False,
                       base_url="https://192.168.1.10:11434", api_key="k")
    cfg_obj.model._effective_providers.return_value = [p]
    monkeypatch.setattr(_cfg._app, "get_config", lambda: cfg_obj)

    seen = {"n": 0}
    _real_tls = _cfg._tls_verify

    def _spy(prov):
        seen["n"] += 1
        return _real_tls(prov)

    monkeypatch.setattr(_cfg, "_tls_verify", _spy)

    captured: dict = {}

    class _FakeResp:
        status_code = 404

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, **kw):
            captured["verify"] = kw.get("verify")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    ep = _endpoint("/api/models", "GET")
    await ep(_req({}))
    assert seen["n"] == 1, f"list_models 没走 _tls_verify 咽喉（被调 {seen['n']} 次）"
    assert captured.get("verify") is True, \
        f"私网 https 未声明 tls_insecure 必须 verify=True（隐式判据已退役）: {captured}"


def test_router_inference_consumes_tls_insecure():
    """★R1（reviewer M3）★：推理主路径接咽喉——tls_insecure=true+私网 https ⇒
    注入 verify=False 的 http client；未声明 ⇒ 不注入（http_client is None）。
    缓存维度：翻转 tls_insecure 必须换缓存键（否则新行为复用旧实例=到不了生产）。
    摘掉 router 里的 `if not _tls_verify(...)` 注入块或缓存键字段，本锁红。"""
    from swarm.models.router import _CHAT_MODEL_CACHE, EndpointProvider
    from swarm.config.settings import ModelConfig

    _CHAT_MODEL_CACHE.clear()
    on = EndpointProvider(
        ProviderConfig(id="local", kind="local", tls_insecure=True,
                       base_url="https://192.168.1.10:11434", api_key="k"),
        ModelConfig())
    off = EndpointProvider(
        ProviderConfig(id="local", kind="local", tls_insecure=False,
                       base_url="https://192.168.1.10:11434", api_key="k"),
        ModelConfig())
    m_on = on.get_chat_model("m1")
    m_off = off.get_chat_model("m1")
    try:
        assert m_on.http_client is not None, "tls_insecure=true+私网 必须注入自定义 http_client"
        assert m_on.http_async_client is not None, "async 侧同样必须注入"
        # ★R2 hunter M1★：只证「有 client」不够——verify=False 被改回默认本锁必须红。
        assert (m_on.http_client._transport._pool._ssl_context.verify_mode
                == ssl.CERT_NONE), "http_client 必须 verify=False（CERT_NONE）"
        assert (m_on.http_async_client._transport._pool._ssl_context.verify_mode
                == ssl.CERT_NONE), "http_async_client 必须 verify=False（CERT_NONE）"
        assert m_off.http_client is None, f"未声明不得注入: {m_off.http_client}"
        assert m_on is not m_off, "tls_insecure 翻转未换缓存键（复用了旧实例）"
    finally:
        import asyncio

        import httpx as _hx
        for m in (m_on, m_off):
            for c in (getattr(m, "http_client", None), getattr(m, "http_async_client", None)):
                if isinstance(c, _hx.Client):
                    c.close()
                elif isinstance(c, _hx.AsyncClient):
                    asyncio.run(c.aclose())  # R2 hunter M1：async client 也显式关，不留给进程退出
        # R2 reviewer L2：缓存里会留着 client 已关闭的实例——同键后续用例会拿到
        # 死 client。测完清账，绝不留给邻居。
        _CHAT_MODEL_CACHE.clear()
