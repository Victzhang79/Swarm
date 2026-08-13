"""D-1（30 号文 HIGH）：provider / notify channel id 服务端 slug 闸——存储型 XSS 提权链的唯一硬底。

根因链：前端 escapeHtml 不转引号（只转 & < >），而 id 被拼进 onclick JS 字符串字面量；
服务端对 id 零字符集校验 ⇒ 非 admin owner（持 config:write）写入含单引号的 provider id，
admin 打开配置页即在 admin 会话上下文执行任意 JS（owner→admin 提权）。
端点闸（值层 host 判据）与本攻击面正交，挡不住。

治：两类用户可写 id 一律过 _SLUG_ID_RE（config/settings.py，形状取自
experience/validation.py:_ID_RE），不合法整单 400，且必须先于 set_secret/persist 任何副作用。

判绿判据（接线证明而非实现正确）：把两个清洗循环里的 slug 检查整块删掉，
本文件所有 400 断言必须全红；副作用零调用断言保证拒绝发生在持久化之前。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from swarm.api.routers import config as _cfg


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
    user.global_role = "admin"
    req.state.user = user
    return req, user


@pytest.fixture
def _harness(monkeypatch):
    """两个端点共用的 mock 面：鉴权放行（admin）、配置为空、持久化/密钥写全间谍化。"""
    user = MagicMock()
    user.username = "alice"
    user.global_role = "admin"
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: True)

    cfg_obj = MagicMock()
    cfg_obj.model._effective_providers.return_value = []
    cfg_obj.notify_channels = []
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


# 覆盖三类逃逸字符（' " <）+ 形状违例（大写/空格/前导连字符/超长/超短/反引号/中文）
_BAD_IDS = [
    "x');alert(1);//",          # 单引号闭合 JS 字符串（D-1 实载）
    'x" onmouseover="alert(1)',  # 双引号闭合 HTML 属性
    "<script>alert(1)</script>",  # 标签注入
    "x`",                        # 反引号（模板字符串上下文）
    "Has Upper",
    "has space",
    "-leading-dash",
    "a",                         # 过短（<2）
    "a" * 65,                    # 超长（>64）
    " ",                         # strip 后为空
    "硅基",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", _BAD_IDS)
async def test_provider_id_slug_gate_rejects_400(_harness, bad_id):
    """provider id 非法 ⇒ 整单 400，且 set_secret/persist 零调用（拒绝先于一切副作用）。"""
    ep = _endpoint("/api/model-providers")
    assert ep is not None
    req, _ = _mk_request({"providers": [
        {"id": bad_id, "base_url": "https://api.example.com/v1", "api_key": "sk-real-key"}]})
    with pytest.raises(HTTPException) as ei:
        await ep(req)
    assert ei.value.status_code == 400, f"非法 id 必须 400: {bad_id!r}"
    assert "非法" in str(ei.value.detail)
    assert _harness["set_secret"] == [], "400 之前绝不可写 secret_store"
    assert _harness["persist"] == [], "400 之前绝不可持久化 .env"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", _BAD_IDS)
async def test_channel_id_slug_gate_rejects_400(_harness, bad_id):
    """notify channel id 非法 ⇒ 整单 400，persist 零调用。"""
    ep = _endpoint("/api/notify-channels")
    assert ep is not None
    req, _ = _mk_request({"channels": [
        {"id": bad_id, "type": "feishu", "webhook_url": "https://hook.example.com/x"}]})
    with pytest.raises(HTTPException) as ei:
        await ep(req)
    assert ei.value.status_code == 400, f"非法 id 必须 400: {bad_id!r}"
    assert "非法" in str(ei.value.detail)
    assert _harness["persist"] == [], "400 之前绝不可持久化 .env"


@pytest.mark.asyncio
@pytest.mark.parametrize("good_id", ["siliconflow", "local", "kimi-code", "ops_1", "a" * 64, "ab"])
async def test_valid_provider_ids_pass(_harness, good_id):
    """合法 slug id 照常放行（不误伤存量：.env 现有 siliconflow/local/kimi-code 全在列）。"""
    ep = _endpoint("/api/model-providers")
    req, _ = _mk_request({"providers": [
        {"id": good_id, "base_url": "https://api.example.com/v1", "api_key": ""}]})
    res = await ep(req)
    assert res["status"] == "ok"
    assert _harness["persist"], "合法请求必须走到持久化"


@pytest.mark.asyncio
async def test_valid_channel_id_passes(_harness):
    ep = _endpoint("/api/notify-channels")
    req, _ = _mk_request({"channels": [
        {"id": "ops_feishu", "type": "feishu", "webhook_url": "https://hook.example.com/x"}]})
    res = await ep(req)
    assert res["status"] == "ok" and res["count"] == 1


@pytest.mark.asyncio
async def test_mixed_list_one_bad_id_rejects_whole_request(_harness):
    """整单 400 语义：列表里一个好 id 一个坏 id ⇒ 整单拒，好 id 也不得落盘。"""
    ep = _endpoint("/api/model-providers")
    req, _ = _mk_request({"providers": [
        {"id": "good-one", "base_url": "https://a.example.com/v1", "api_key": ""},
        {"id": "bad'id", "base_url": "https://b.example.com/v1", "api_key": ""}]})
    with pytest.raises(HTTPException) as ei:
        await ep(req)
    assert ei.value.status_code == 400
    assert _harness["persist"] == [], "整单拒绝：好 id 也不许部分落盘"


# ── hunter HIGH-1：PUT /api/config 直写结构化键必须整单 400（slug 闸唯一入口）──

@pytest.fixture
def _config_harness(monkeypatch):
    """update_config 的 mock 面：鉴权放行、落盘sink间谍化。
    ★OPS-1 教训★：400 正常时先于写盘，但删闸突变会让载荷走【真实写盘路径】——
    atomic_write_env 必须永远 mock，否则突变实验会把测试载荷写进真实 .env（已发生一次）。"""
    user = MagicMock()
    user.username = "alice"
    user.global_role = "admin"
    monkeypatch.setattr(_cfg, "_require_perm", lambda *a, **k: user)
    written: list = []
    monkeypatch.setattr(_cfg, "atomic_write_env",
                        lambda path, content: written.append((path, content)))
    return user


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [
    "SWARM_MODEL_PROVIDERS", "SWARM_NOTIFY_CHANNELS",
    "swarm_model_providers", "Swarm_Model_Providers",   # R1 复核 HIGH：大小写变体
    "swarm_notify_channels",
])
@pytest.mark.parametrize("is_admin", [True, False])
@pytest.mark.parametrize("envelope", [True, False])
async def test_update_config_rejects_direct_structured_key_write(_config_harness, monkeypatch,
                                                                 key, is_admin, envelope):
    """hunter HIGH-1 回归锁：直写两键 → 400（admin 同拒——slug 闸只有一个入口）。
    R1 复核增补：pydantic case_sensitive=False ⇒ 小写键同样绑定 providers，大小写变体必须同拒。
    删掉 update_config 里的 _STRUCTURED_SLUG_GATED_KEYS 检查（或摘掉 .upper()），本测试必须红。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: is_admin)
    payload = {key: '[{"id": "x\');alert(1);//", "base_url": "https://api.siliconflow.cn/v1"}]'}
    body = {"config": payload} if envelope else payload
    ep = _endpoint("/api/config")
    assert ep is not None
    req, _ = _mk_request(body)
    with pytest.raises(HTTPException) as ei:
        await ep(req)
    assert ei.value.status_code == 400
    assert "专用端点" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_update_config_still_accepts_flat_keys(_config_harness, monkeypatch, tmp_path):
    """不误伤：普通扁平键照常走（写盘面全 mock，只验不被 400 误拦）。"""
    monkeypatch.setattr(_cfg, "_caller_is_admin", lambda _u: True)
    monkeypatch.setattr(_cfg._app, "_PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(_cfg._app, "reload_config", lambda: None)
    cfg_obj = MagicMock()
    cfg_obj.model_dump.return_value = {}
    monkeypatch.setattr(_cfg._app, "get_config", lambda: cfg_obj)
    monkeypatch.setattr(_cfg, "_mask_config_dict", lambda d: d)
    monkeypatch.setattr(_cfg, "_reject_endpoint_keys",
                        lambda m, is_admin, who, rejected_out=None: m)
    ep = _endpoint("/api/config")
    req, _ = _mk_request({"SWARM_MODEL_WORKER_PRIMARY": "some-model"})
    res = await ep(req)
    assert res["status"] == "ok", f"扁平键不许被误拦: {res}"


# ── hunter LOW-2：闸拒绝必须留 WARNING 痕（防御侧信号）──

@pytest.mark.asyncio
async def test_slug_rejection_emits_warning(_harness, caplog):
    import logging
    ep = _endpoint("/api/model-providers")
    req, _ = _mk_request({"providers": [{"id": "bad'id", "api_key": ""}]})
    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException):
            await ep(req)
    assert any(r.levelno >= logging.WARNING and "slug 闸" in r.getMessage()
               for r in caplog.records), "拒绝必须留 WARNING（防御侧信号）"
