"""30 号文批19 config 面合批锁：D-1b fixed_temperature 保留 / D-1c model_providers 值闸
/ D-1d 缺 id 条目留痕 / G-1b 扁平回写方向性 / D-1e settings 层 slug validator。

- D-1b：clean entry 白名单丢 `fixed_temperature`（ProviderConfig:169 有字段、
  router.py:1069 有消费者、唯独写入端点不认）⇒ 现网 kimi-code 的 fixed_temperature=1.0
  被 UI 重存静默丢掉。锁=端到端断 persisted JSON 里字段在。
- D-1c：model_providers 映射【值】是 provider id，原实现零校验直落 .env。
- D-1d：缺 id/非对象条目静默 skip 与整单 400 语义不一致——行为不变但须 WARNING 留痕。
- G-1b（拍板=方向性判定放开）：扁平回写仅在 base_url 真变时塞 *_URL 键——非 admin
  只换 key 不再 403（与 kb 端点一致），动 URL 仍 403。
- D-1e：settings 层 field_validator 是真单一咽喉（覆盖手编 .env/未来新写端点）。
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from swarm.api.routers import config as _cfg
from swarm.config.settings import NotifyChannel, ProviderConfig


def _endpoint(path: str, method: str = "PUT"):
    for r in _cfg.router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    return None


def _harness(monkeypatch, *, admin: bool, existing: list | None = None):
    """与 test_d1 同形 mock 面：鉴权/配置/持久化/密钥写全间谍化，真闸 _reject_endpoint_keys 照跑。"""
    user = MagicMock()
    user.username = "alice"
    user.global_role = "admin" if admin else "owner"
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: admin)

    cfg_obj = MagicMock()
    cfg_obj.model._effective_providers.return_value = existing or []
    cfg_obj.notify_channels = []
    # reviewer R1-M1 折入后回写比较对象是 flat 现值 cur.{id}_base_url——
    # MagicMock 属性是对象不是串，不设就恒「真变」⇒ G-1b 主锁假红。
    # 夹具必须把 flat 摆成与 existing 一致（正常态）；stale 测试自己改它。
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
    return calls


def _req(body):
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    return req


def _persisted_update_map(calls) -> dict:
    """_persist_env_updates(update_map, is_admin=..., who=...) 的第一实参。"""
    assert calls["persist"], "persist 未被调用"
    return calls["persist"][-1][0][0]


# ── D-1b：fixed_temperature 端到端保留 ─────────────────────

@pytest.mark.asyncio
async def test_fixed_temperature_preserved_end_to_end(monkeypatch):
    """D-1b：provider 带 fixed_temperature 提交，persisted providers JSON 必须含该字段。
    删掉端点里的 fixed_temperature 保留块本锁红。"""
    calls = _harness(monkeypatch, admin=True)
    ep = _endpoint("/api/model-providers")
    body = {"providers": [{"id": "kimi-code", "kind": "cloud",
                           "base_url": "https://api.kimi.example/v1",
                           "api_key": "sk-x", "fixed_temperature": 1.0}]}
    await ep(_req(body))
    persisted = json.loads(_persisted_update_map(calls)["SWARM_MODEL_PROVIDERS"])
    assert persisted[0]["fixed_temperature"] == 1.0, \
        f"fixed_temperature 被写入端点静默丢弃: {persisted[0]}"


@pytest.mark.asyncio
async def test_fixed_temperature_absent_when_not_given(monkeypatch):
    """反向钉：未给 fixed_temperature 时字段不出现在 JSON（None 不落盘，老行为不变）。"""
    calls = _harness(monkeypatch, admin=True)
    ep = _endpoint("/api/model-providers")
    await ep(_req({"providers": [{"id": "siliconflow", "kind": "cloud",
                                  "base_url": "https://api.siliconflow.cn/v1",
                                  "api_key": "sk-y"}]}))
    persisted = json.loads(_persisted_update_map(calls)["SWARM_MODEL_PROVIDERS"])
    assert "fixed_temperature" not in persisted[0]


# ── D-1c：model_providers 值层 slug 闸 ─────────────────────

@pytest.mark.asyncio
async def test_model_providers_bad_value_400(monkeypatch):
    """D-1c：映射值必须是合法 provider id（slug），非法整单 400 且先于任何副作用。"""
    calls = _harness(monkeypatch, admin=True)
    ep = _endpoint("/api/model-providers")
    with pytest.raises(HTTPException) as ei:
        await ep(_req({"model_providers": {"kimi-for-coding": "x');alert(1);//"}}))
    assert ei.value.status_code == 400
    assert calls["persist"] == [] and calls["set_secret"] == []


@pytest.mark.asyncio
async def test_model_providers_legit_value_passes(monkeypatch):
    """反向钉：合法 provider id 值不误杀。"""
    calls = _harness(monkeypatch, admin=True)
    ep = _endpoint("/api/model-providers")
    await ep(_req({"model_providers": {"kimi-for-coding": "siliconflow"}}))
    persisted = json.loads(_persisted_update_map(calls)["SWARM_MODEL_MODEL_PROVIDERS"])
    assert persisted == {"kimi-for-coding": "siliconflow"}


# ── D-1d：缺 id 条目 WARNING 留痕（行为不变仍 skip）────────

@pytest.mark.asyncio
async def test_entry_without_id_warns_and_skips(monkeypatch, caplog):
    """D-1d：缺 id 条目被 skip 时必须 WARNING（零机读信号族）；合法条目照常处理。"""
    calls = _harness(monkeypatch, admin=True)
    ep = _endpoint("/api/model-providers")
    with caplog.at_level(logging.WARNING):
        await ep(_req({"providers": [
            {"label": "没 id 的条目"},
            {"id": "siliconflow", "kind": "cloud",
             "base_url": "https://api.siliconflow.cn/v1", "api_key": "sk-z"},
        ]}))
    assert any("D-1d" in r.message for r in caplog.records), \
        f"缺 id 条目静默 skip 零留痕: {[r.message for r in caplog.records]}"
    persisted = json.loads(_persisted_update_map(calls)["SWARM_MODEL_PROVIDERS"])
    assert [p["id"] for p in persisted] == ["siliconflow"]


# ── G-1b：扁平回写方向性（拍板=只换 key 放行，动 URL 仍 403）──

@pytest.mark.asyncio
async def test_key_only_change_nonadmin_no_403(monkeypatch):
    """G-1b 主锁：非 admin 对内置 provider 只换 api_key（base_url 不变）——
    update_map 不得出现 *_BASE_URL 键 ⇒ 真闸 _reject_endpoint_keys 无由 403。
    退回「无条件塞 *_URL 键」本锁红。"""
    calls = _harness(monkeypatch, admin=False, existing=[
        ProviderConfig(id="siliconflow", kind="cloud",
                       base_url="https://api.siliconflow.cn/v1", api_key="sk-old"),
    ])
    # 值层闸的旧集合来自进程 env 的现值（config.py:126）——夹具必须把「当前 .env」
    # 摆成同一 base_url（key 已脱密落 db 的真实形态），否则换 key 也被误判引入新 host。
    monkeypatch.setenv("SWARM_MODEL_PROVIDERS", json.dumps(
        [{"id": "siliconflow", "kind": "cloud",
          "base_url": "https://api.siliconflow.cn/v1", "api_key": ""}],
        ensure_ascii=False))
    ep = _endpoint("/api/model-providers")
    await ep(_req({"providers": [{"id": "siliconflow", "kind": "cloud",
                                  "base_url": "https://api.siliconflow.cn/v1",
                                  "api_key": "sk-new"}]}))
    um = _persisted_update_map(calls)
    assert "SWARM_MODEL_SILICONFLOW_BASE_URL" not in um, \
        f"base_url 未变却塞了 *_URL 键（G-1b 回潮）: {sorted(um)}"
    assert calls["set_secret"], "换 key 仍应写 secret_store"


@pytest.mark.asyncio
async def test_base_url_change_nonadmin_still_403(monkeypatch):
    """G-1b 方向钉：非 admin 真改 base_url ⇒ *_URL 键进 update_map ⇒ 403 不误放。"""
    calls = _harness(monkeypatch, admin=False, existing=[
        ProviderConfig(id="siliconflow", kind="cloud",
                       base_url="https://api.siliconflow.cn/v1", api_key="sk-old"),
    ])
    ep = _endpoint("/api/model-providers")
    with pytest.raises(HTTPException) as ei:
        await ep(_req({"providers": [{"id": "siliconflow", "kind": "cloud",
                                      "base_url": "https://evil.example/v1",
                                      "api_key": "sk-new"}]}))
    assert ei.value.status_code == 403
    assert calls["persist"] == [] and calls["set_secret"] == []


@pytest.mark.asyncio
async def test_stale_flat_key_self_heals(monkeypatch):
    """reviewer R1-M1：flat 与 providers JSON 脱节（flat=旧 host）时，只换 key
    也必须回写 *_URL 键让老读取点（preprocess/norms_inference/app 三处）自愈。
    回写比较对象退回 providers JSON 旧值（old_by_id）本锁红。"""
    calls = _harness(monkeypatch, admin=True, existing=[
        ProviderConfig(id="local", kind="local",
                       base_url="http://new-host/api", api_key="sk-old"),
    ])
    # 摆脱节态：flat 现值仍是旧 host
    cfg_obj = _cfg._app.get_config()
    cfg_obj.model.local_base_url = "http://old-host/api"
    ep = _endpoint("/api/model-providers")
    await ep(_req({"providers": [{"id": "local", "kind": "local",
                                  "base_url": "http://new-host/api",
                                  "api_key": "sk-new"}]}))
    um = _persisted_update_map(calls)
    assert um.get("SWARM_MODEL_LOCAL_BASE_URL") == "http://new-host/api", \
        f"flat 脱节时回写缺失（stale 不自愈）: {sorted(um)}"


@pytest.mark.asyncio
async def test_flat_stale_nonadmin_key_only_no_403(monkeypatch):
    """hunter R1-M1：flat 脱节 + 非 admin + 只换 key——自愈写键只对 admin 开放，
    非 admin 不得被误 403（拍板「只换 key 放行」在脱节态同样成立）。
    回写条件退回「flat 现值单条件」（reviewer R1-M1 折法）本锁红。"""
    calls = _harness(monkeypatch, admin=False, existing=[
        ProviderConfig(id="siliconflow", kind="cloud",
                       base_url="https://api.siliconflow.cn/v1", api_key="sk-old"),
    ])
    # 脱节态：flat 现值是旧 host（ providers JSON 里是现行 host）
    cfg_obj = _cfg._app.get_config()
    cfg_obj.model.siliconflow_base_url = "http://stale-old/api"
    # 值层闸旧集合摆成同 host（本命题与值层无关，别让它先 403）
    monkeypatch.setenv("SWARM_MODEL_PROVIDERS", json.dumps(
        [{"id": "siliconflow", "kind": "cloud",
          "base_url": "https://api.siliconflow.cn/v1", "api_key": ""}],
        ensure_ascii=False))
    ep = _endpoint("/api/model-providers")
    await ep(_req({"providers": [{"id": "siliconflow", "kind": "cloud",
                                  "base_url": "https://api.siliconflow.cn/v1",
                                  "api_key": "sk-new"}]}))  # 不抛 403 即过闸
    um = _persisted_update_map(calls)
    assert "SWARM_MODEL_SILICONFLOW_BASE_URL" not in um, \
        f"非 admin 脱节态被误塞 *_URL 键（hunter R1-M1 误杀回潮）: {sorted(um)}"


@pytest.mark.asyncio
async def test_fixed_temperature_invalid_value_warns(monkeypatch, caplog):
    """reviewer R1-M2：fixed_temperature 非法值不阻断但必须 WARNING（与 D-1d 对称）。"""
    calls = _harness(monkeypatch, admin=True)
    ep = _endpoint("/api/model-providers")
    with caplog.at_level(logging.WARNING):
        await ep(_req({"providers": [{"id": "kimi-code", "kind": "cloud",
                                      "base_url": "https://api.kimi.example/v1",
                                      "api_key": "sk-x", "fixed_temperature": "auto"}]}))
    assert any("D-1b" in r.message and "auto" in r.message for r in caplog.records), \
        f"非法 fixed_temperature 静默丢弃: {[r.message for r in caplog.records]}"
    persisted = json.loads(_persisted_update_map(calls)["SWARM_MODEL_PROVIDERS"])
    assert "fixed_temperature" not in persisted[0]


# ── D-1e：settings 层 slug validator（真单一咽喉）──────────

def test_provider_config_id_validator():
    """D-1e：手编 .env 路径（不经端点闸）也过不了 settings 层——非 slug 非空即拒，
    空串（未设置）放行。删掉 ProviderConfig 的 field_validator 本锁红。"""
    with pytest.raises(Exception):
        ProviderConfig(id="x');alert(1);//")
    with pytest.raises(Exception):
        ProviderConfig(id="Has Upper")
    assert ProviderConfig(id="").id == ""
    assert ProviderConfig(id="siliconflow").id == "siliconflow"


def test_notify_channel_id_validator():
    """D-1e 同闸第二类：NotifyChannel。"""
    with pytest.raises(Exception):
        NotifyChannel(id="ch1' onclick=alert(1)")
    assert NotifyChannel(id="").id == ""
    assert NotifyChannel(id="ch1").id == "ch1"
