"""G-1（30 号文 HIGH）：set_secret 必须在 403 裁决【之后】——运行时锁。

历史翻车（本锁的立项理由）：注释自称「裁决必须在任何副作用之前（复核 HIGH）」、测试
test_s1_endpoint_gate_value_layer.py 自称已锁——但它 getsource 断的是 gate vs persist 序对
（两行都在 set_secret 之后，恒真），git 溯源 9aba4a2 起副作用先于裁决的顺序从未变过。
非 admin 一次 403 请求即可经 set_secret upsert 覆盖全部 provider 凭据（不可恢复）。
kb embed/rerank 端点同型第二例：连 403 都没有，backstop 静默剔键回 200 partial。

治本（本文件的判绿判据——把 set_secret 移回闸前/把 403 判空，对应测试必须红）：
1. providers：update_map 用脱 key 副本序列化（明文绝不经 update_map 落盘），裁决 403 →
   set_secret → persist；db 写失败回退明文进 .env（原「不丢配置」承诺不变）。
2. kb：端点层补 _reject_endpoint_keys + 403（与 model-providers 对齐「拒绝不伪装成成功」），
   pending secrets 裁决后才落。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from swarm.api.routers import config as _cfg

_OLD_PROVIDERS = '[{"id":"siliconflow","base_url":"https://api.siliconflow.cn/v1","api_key":""}]'


def _endpoint(path: str, method: str = "PUT"):
    for r in _cfg.router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    return None


def _mk_request(body):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    user = MagicMock()
    user.username = "alice"
    user.global_role = "owner"
    req.state.user = user
    return req, user


@pytest.fixture
def _spy(monkeypatch):
    """两端点共用间谍面：鉴权可切 + gate/set_secret/persist 全记录调用序列（落盘 sink 全 mock——
    突变实验会把没 mock 的 sink 点亮，OPS-1 教训）。"""
    seq: list = []
    user = MagicMock()
    user.username = "alice"
    user.global_role = "owner"
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)

    real_gate = _cfg._reject_endpoint_keys

    def _gate_rec(update_map, is_admin, who, rejected_out=None):
        seq.append(("gate",))
        return real_gate(update_map, is_admin, who, rejected_out=rejected_out)

    monkeypatch.setattr(_cfg, "_reject_endpoint_keys", _gate_rec)
    import swarm.config.secret_store as _ss
    monkeypatch.setattr(_ss, "set_secret",
                        lambda name, val, **k: seq.append(("set_secret", name, val)))
    monkeypatch.setattr(_ss, "invalidate_cache", lambda *a, **k: None)
    monkeypatch.setattr(
        _cfg, "_persist_env_updates",
        lambda m, **k: seq.append(("persist", dict(m))) or {"audit_status": "ok"})
    monkeypatch.setattr(_cfg, "atomic_write_env", lambda path, content: None)
    return seq


def _mock_cur_config(monkeypatch):
    cfg_obj = MagicMock()
    cfg_obj.model._effective_providers.return_value = []
    monkeypatch.setattr(_cfg._app, "get_config", lambda: cfg_obj)


# ── providers 端点 ──

@pytest.mark.asyncio
async def test_providers_403_zero_secret_side_effect(_spy, monkeypatch):
    """★G-1 核心不变量★：非 admin 提交恶意 base_url + 垃圾 key → 403，且 set_secret/persist 零调用。
    把 set_secret 移回闸前，本测试必红。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: False)
    monkeypatch.setenv("SWARM_MODEL_PROVIDERS", _OLD_PROVIDERS)
    _mock_cur_config(monkeypatch)
    ep = _endpoint("/api/model-providers")
    req, _ = _mk_request({"providers": [
        {"id": "siliconflow", "base_url": "http://attacker.example/v1", "api_key": "GARBAGE_KEY"}]})
    with pytest.raises(HTTPException) as ei:
        await ep(req)
    assert ei.value.status_code == 403 and "被拒键" in str(ei.value.detail)
    kinds = [s[0] for s in _spy]
    assert "set_secret" not in kinds, "403 时 set_secret 必须零调用（原洞：闸前覆盖真 key）"
    assert "persist" not in kinds, "403 时 .env 必须零写"


@pytest.mark.asyncio
async def test_providers_admin_order_gate_before_set_secret(_spy, monkeypatch):
    """放行路径的运行序锁：gate < set_secret < persist；且落盘 JSON 必须脱 key。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: True)
    _mock_cur_config(monkeypatch)
    monkeypatch.setattr("swarm.models.router.ModelRouter",
                        lambda: MagicMock(get_routing_table=lambda: {}))
    ep = _endpoint("/api/model-providers")
    req, _ = _mk_request({"providers": [
        {"id": "siliconflow", "base_url": "https://api.siliconflow.cn/v1", "api_key": "real-key-1"}]})
    res = await ep(req)
    assert res["status"] == "ok"
    kinds = [s[0] for s in _spy]
    assert kinds.index("gate") < kinds.index("set_secret") < kinds.index("persist"), \
        f"运行序必须是 gate→set_secret→persist: {kinds}"
    import json as _j
    persisted = next(s[1] for s in _spy if s[0] == "persist")
    prov = _j.loads(persisted["SWARM_MODEL_PROVIDERS"])
    assert prov[0]["api_key"] == "", "落盘 JSON 绝不留明文 key"
    assert ("set_secret", "provider_api_key:siliconflow", "real-key-1") in _spy


@pytest.mark.asyncio
async def test_providers_secret_failure_falls_back_plaintext(_spy, monkeypatch):
    """db 写失败回退明文进 .env（「不丢配置」承诺不因重排序而破）。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: True)
    _mock_cur_config(monkeypatch)
    monkeypatch.setattr("swarm.models.router.ModelRouter",
                        lambda: MagicMock(get_routing_table=lambda: {}))
    import swarm.config.secret_store as _ss

    def _boom(name, val, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(_ss, "set_secret", _boom)
    ep = _endpoint("/api/model-providers")
    req, _ = _mk_request({"providers": [
        {"id": "siliconflow", "base_url": "https://api.siliconflow.cn/v1", "api_key": "real-key-1"}]})
    res = await ep(req)
    assert res["status"] == "ok", "secret_store 失败不得让配置丢失"
    import json as _j
    persisted = next(s[1] for s in _spy if s[0] == "persist")
    assert _j.loads(persisted["SWARM_MODEL_PROVIDERS"])[0]["api_key"] == "real-key-1", \
        "回退路径必须把 key 明文写回 .env（否则配置静默丢失）"


@pytest.mark.asyncio
async def test_providers_fallback_key_with_url_survives_backstop(_spy, monkeypatch):
    """★hunter MEDIUM-1（30 号文施治期）★：回退明文 JSON 里 api_key 恰含 "://" 时，
    persist backstop 的二次裁决不得把它冤判为新出站端点——原实现下非 admin 整键被剔除
    而端点已回 200 + updated_keys，配置静默蒸发。可达条件：【自定义 provider】（内置
    siliconflow/local 的扁平回写键会先被键名分类器 403，走不到 backstop）+ 非 admin +
    host 未变 + db 故障 + 凭据内嵌 URL。本测试让 backstop【真实执行】（落盘仍 mock——
    OPS-1 纪律）。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: False)
    monkeypatch.setenv("SWARM_MODEL_PROVIDERS",
                       '[{"id":"kimi-code","base_url":"https://api.kimi.com/coding/v1","api_key":""}]')
    _mock_cur_config(monkeypatch)
    monkeypatch.setattr("swarm.models.router.ModelRouter",
                        lambda: MagicMock(get_routing_table=lambda: {}))
    import swarm.config.secret_store as _ss

    def _boom(name, val, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(_ss, "set_secret", _boom)
    # backstop 真实跑（fixture 的 gate 记录器内部即真闸）；落盘/审计不触——只记录存活键。
    survived: list = []
    _gate = _cfg._reject_endpoint_keys

    def _persist_spy(m, **k):
        kept = _gate(dict(m), k.get("is_admin", False), "persist_env")
        survived.append(sorted(kept))
        return {"audit_status": "ok"}
    monkeypatch.setattr(_cfg, "_persist_env_updates", _persist_spy)
    ep = _endpoint("/api/model-providers")
    req, _ = _mk_request({"providers": [
        {"id": "kimi-code", "base_url": "https://api.kimi.com/coding/v1",
         "api_key": "gw-token-https://internal.gateway/x"}]})
    res = await ep(req)
    assert res["status"] == "ok"
    assert survived == [["SWARM_MODEL_PROVIDERS"]], \
        f"回退明文被 backstop 冤杀＝配置静默丢失（端点却回了 200）: {survived}"


# ── kb embed/rerank 端点（第二例）──

@pytest.mark.asyncio
async def test_kb_403_zero_secret_side_effect(_spy, monkeypatch):
    """第二例对齐：非 admin 改 embed base_url + 垃圾 key → 403（原来回 200 partial），
    set_secret/persist 零调用。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: False)
    monkeypatch.setenv("SWARM_KB_EMBED_BASE_URL", "https://old-embed.example/v1")
    ep = _endpoint("/api/kb/embed-rerank")
    req, _ = _mk_request({"embed": {
        "base_url": "http://evil-embed.example/v1", "api_key": "GARBAGE"}})
    with pytest.raises(HTTPException) as ei:
        await ep(req)
    assert ei.value.status_code == 403 and "被拒键" in str(ei.value.detail), \
        "kb 端点必须 403 明示（原实现 200 partial 伪装成功）"
    kinds = [s[0] for s in _spy]
    assert "set_secret" not in kinds and "persist" not in kinds


@pytest.mark.asyncio
async def test_kb_non_admin_key_only_change_allowed(_spy, monkeypatch):
    """凭据本身走 config:write 语义（B8-F2 设计）：非 admin 只改 api_key（不动端点）→ 放行。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: False)
    ep = _endpoint("/api/kb/embed-rerank")
    req, _ = _mk_request({"embed": {"api_key": "new-embed-key"}})
    res = await ep(req)
    # 复核 LOW：响应键是 embed_model_changed，"dim_changed" 永不出现——or 分支是死代码。
    assert res.get("status") == "ok", f"只改 key 不应被拒: {res}"
    from swarm.knowledge.embed_rerank_config import SECRET_EMBED_KEY
    assert ("set_secret", SECRET_EMBED_KEY, "new-embed-key") in _spy


@pytest.mark.asyncio
async def test_kb_admin_order_gate_before_set_secret(_spy, monkeypatch):
    """kb 放行路径运行序锁：gate < set_secret < persist。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: True)
    ep = _endpoint("/api/kb/embed-rerank")
    req, _ = _mk_request({"embed": {
        "base_url": "http://new-embed.example/v1", "api_key": "embed-key-1"}})
    res = await ep(req)
    assert res.get("status") == "ok"
    kinds = [s[0] for s in _spy]
    assert kinds.index("gate") < kinds.index("set_secret") < kinds.index("persist"), \
        f"运行序必须是 gate→set_secret→persist: {kinds}"
