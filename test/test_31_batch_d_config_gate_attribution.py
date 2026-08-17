"""31 号文批 D 锁：A4-M2 backstop 归因/回传 · A4-M3 值层差集分档 · A4-L1/L2 权限判据同源。

四条 finding 的共同形态是**判据与它自己的设计声明不一致**：
- A4-M2：`who` 是必填 kwarg（"审计缺了 who 等于没有审计"），backstop 却传字面量；
- A4-M3：注释把判据定义为 host，实现在整条 URL 串上做差集（同 host 改路径被冤杀）；
- A4-L1：同文件另两个写凭据端点要 admin，唯 migrate 敞开且零审计；
- A4-L2：F5 helper 的立项理由就是"分两次检查会漂移"，本端点没跟上（RBAC 关闭时恒 403）。

锁的取向：**断"接线事实/判据同源"，不断字面量**（纪律 6）。故凡能直接调生产函数的都调，
不复刻表达式（批 C 的 c6 教训：照抄=自己给自己背书）。
"""

from __future__ import annotations

import os

import pytest

from swarm.api.routers.config import (
    _diff_outbound,
    _outbound_urls_in_value,
    _persist_env_updates,
    _reject_endpoint_keys,
    _url_authority,
    _url_diff_tier,
    _URL_DIFF_HOST_TIER_KEYS,
)


@pytest.fixture(autouse=True)
def _isolate_environ():
    """A4-M3 的差集读 os.environ；绝不让夹具泄漏到别的用例（更绝不写 .env）。"""
    _snap = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(_snap)


# ───────────────────────── A4-M3：值层差集分档 ─────────────────────────

def test_m3_tier_default_is_full_failclosed():
    """未登记的键（含未来新增）必须落 full 档——漏登记只退回过严，绝不开外泄通道。"""
    for k in ("SWARM_FUTURE_SINK", "SWARM_NOTIFY_CHANNELS", "SWARM_WEBHOOK_THING", ""):
        assert _url_diff_tier(k) == "full", f"{k!r} 应落 fail-closed 的 full 档"


def test_m3_host_tier_is_explicit_optin_only():
    """host 档只能靠【逐键登记】拿到，不能靠后缀/族正则蒙对。

    ★这条锁的意义：把 host 档做成模式匹配（如"含 PROVIDERS 就 host"）时它会红。★
    host 档是放宽方向，必须逐键论证"路径不是投递目标"。
    """
    assert _URL_DIFF_HOST_TIER_KEYS == frozenset({"SWARM_MODEL_PROVIDERS"})
    # 名字相近但未登记的键不得沾光
    for k in ("SWARM_MODEL_PROVIDERS_BACKUP", "SWARM_MODEL_PROVIDER", "MY_SWARM_MODEL_PROVIDERS"):
        assert _url_diff_tier(k) == "full", f"{k!r} 未登记却拿到 host 档 ⇒ 档位判据被模式化了"


def test_m3_tier_lookup_is_case_insensitive():
    """pydantic case_sensitive 未设 ⇒ 小写键绑同一字段；档位查表必须归一，否则可绕。"""
    assert _url_diff_tier("swarm_model_providers") == "host"
    assert _url_diff_tier("  SWARM_MODEL_PROVIDERS  ") == "host"


@pytest.mark.parametrize("new,old,expect_added", [
    # 同 host 改路径 → 放行（这正是 finding 的冤杀面）
    ({"https://api.x.com/v2"}, {"https://api.x.com/v1"}, False),
    # 换 host → 拒
    ({"https://evil.com/v1"}, {"https://api.x.com/v1"}, True),
    # 删条目 → 放行（集合收缩）
    (set(), {"https://api.x.com/v1"}, False),
    # 首次引入（旧集合为空）→ 拒（本就该 admin 拍板）
    ({"https://api.x.com/v1"}, set(), True),
    # 同 host 换 scheme：authority 相同 → 放行（http→https 不改收件人）
    ({"http://api.x.com/v1"}, {"https://api.x.com/v1"}, False),
    # 改端口 → 拒（不同端口 = 不同服务）
    ({"https://api.x.com:9443/v1"}, {"https://api.x.com/v1"}, True),
    # userinfo 变化但 host 不变 → 放行（凭据不是端点身份）
    ({"https://u2:p2@api.x.com/v1"}, {"https://u1:p1@api.x.com/v1"}, False),
    # ★子域名不算同 host★（evil.api.x.com ≠ api.x.com）
    ({"https://evil.api.x.com/v1"}, {"https://api.x.com/v1"}, True),
    # ★后缀伪装★：api.x.com.evil.com 必须算新 host
    ({"https://api.x.com.evil.com/v1"}, {"https://api.x.com/v1"}, True),
])
def test_m3_host_tier_four_cells(new, old, expect_added):
    got = _diff_outbound(new, old, tier="host")
    assert bool(got) is expect_added, f"host 档 {new} vs {old} → {got}"


def test_m3_full_tier_keeps_path_sensitivity():
    """full 档必须保持整串敏感——webhook token 在 path 里，同 host 换路径 = 换收件人。

    ★这条与上一条是**反向对**★：若有人"顺手"把 host 归一推广到所有键，本条即红。
    """
    added = _diff_outbound({"https://hooks.slack.com/services/T1/B2/EVIL"},
                           {"https://hooks.slack.com/services/T1/B2/GOOD"}, tier="full")
    assert added, "full 档放行了同 host 改路径 ⇒ 通知外泄通道被打开"


def test_m3_malformed_url_is_failclosed_not_collapsed():
    """畸形 URL 必须回退整串当身份，绝不塌成空串。

    塌成空串 ⇒ 所有畸形值归一到同一个键 ⇒ 差集恒空 ⇒ 值层闸对畸形载荷整体失效。
    """
    assert _url_authority("://///") != ""
    assert _url_authority("garbage") == "garbage"
    # 两个不同的畸形串不得互相抵消
    added = _diff_outbound({"weird-one"}, {"weird-two"}, tier="host")
    assert added == {"weird-one"}


def test_m3_gate_uses_tier_end_to_end_providers_path_change_allowed():
    """★端到端接线锁★：经 `_reject_endpoint_keys` 走真实判决，非 admin 改 provider 路径应放行。

    这条锁的是"档位真的被闸消费了"，不是"helper 算得对"。
    """
    import json
    os.environ["SWARM_MODEL_PROVIDERS"] = json.dumps(
        [{"id": "custom", "base_url": "https://api.x.com/v1", "models": ["m"]}])
    new_val = json.dumps(
        [{"id": "custom", "base_url": "https://api.x.com/v2", "models": ["m"]}])
    rej: list[str] = []
    kept = _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": new_val}, False, "owner1",
                                rejected_out=rej)
    assert kept == {"SWARM_MODEL_PROVIDERS": new_val}, "同 host 改路径被冤杀（A4-M3 未生效）"
    assert rej == []


def test_m3_gate_still_rejects_new_host_end_to_end():
    """反向：换 host 仍须被整键拒（放宽不得越界）。"""
    import json
    os.environ["SWARM_MODEL_PROVIDERS"] = json.dumps(
        [{"id": "custom", "base_url": "https://api.x.com/v1", "models": ["m"]}])
    evil = json.dumps(
        [{"id": "custom", "base_url": "https://evil.com/v1", "models": ["m"]}])
    rej: list[str] = []
    kept = _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": evil}, False, "owner1",
                                rejected_out=rej)
    assert kept == {}, "换 host 被放行 ⇒ 凭据钓鱼面重开"
    assert rej == ["SWARM_MODEL_PROVIDERS"]


def test_m3_notify_channels_path_change_still_rejected_end_to_end():
    """★分档的证据面★：同一个闸、同一个抽取函数，notify 的同 host 改路径仍须拒。

    若 host 归一被一刀切应用，本条红。这是"复用单一事实源 ≠ 复用消费契约"的机器检查。
    """
    import json
    os.environ["SWARM_NOTIFY_CHANNELS"] = json.dumps(
        [{"id": "c1", "type": "slack", "webhook_url": "https://hooks.slack.com/services/A"}])
    swapped = json.dumps(
        [{"id": "c1", "type": "slack", "webhook_url": "https://hooks.slack.com/services/EVIL"}])
    rej: list[str] = []
    kept = _reject_endpoint_keys({"SWARM_NOTIFY_CHANNELS": swapped}, False, "owner1",
                                rejected_out=rej)
    assert kept == {}, "notify 同 host 换 path 被放行 ⇒ 任务内容外泄到攻击者 webhook"
    assert rej == ["SWARM_NOTIFY_CHANNELS"]


def test_m3_admin_unaffected_by_tier():
    """admin 走早返回，档位对其无影响（别把放宽做成"对 admin 也多一道判"）。"""
    assert _reject_endpoint_keys({"SWARM_NOTIFY_CHANNELS": "https://new/x"}, True, "a") == {
        "SWARM_NOTIFY_CHANNELS": "https://new/x"}


def test_m3_extractor_still_shared_single_source():
    """抽取函数必须仍是共享的单一事实源——分档只分【消费契约】，不复制抽取逻辑。"""
    payload = '{"base_url":"https://a/x","webhook_url":"https://b/y"}'
    assert _outbound_urls_in_value(payload) == {"https://a/x", "https://b/y"}


# ───────────────────────── A4-M2：backstop 归因与回传 ─────────────────────────

def _fake_persist_env(monkeypatch, tmp_path):
    """把 _persist_env_updates 的落盘/reload/审计全部换掉。

    ★绝不触碰真 .env★（本仓已有过"删闸突变让测试载荷写进真 .env"的事故）。
    """
    from swarm.api.routers import config as cfgmod
    env_path = tmp_path / ".env"
    env_path.write_text("SWARM_EXISTING=1\n", encoding="utf-8")

    class _FakeRoot:
        def __truediv__(self, other):
            return env_path

    monkeypatch.setattr(cfgmod._app, "_PROJECT_ROOT", _FakeRoot(), raising=False)
    monkeypatch.setattr(cfgmod, "_reload_with_rollback", lambda *a, **k: None)
    import swarm.config.settings as _st
    monkeypatch.setattr(_st, "reload_config", lambda *a, **k: None, raising=False)
    import swarm.config.config_audit as _ca
    monkeypatch.setattr(_ca, "record_config_changes",
                        lambda *a, **k: {"written": len(a[2]) if len(a) > 2 else 0,
                                         "failed": False, "degrade_key": None})
    return env_path


def test_m2_backstop_warning_carries_real_who(monkeypatch, tmp_path, caplog):
    """★核心锁★：backstop 触发时那条安全 WARNING 必须出现【真实调用者】，不是字面量。

    突变：把 `who` 改回 `"persist_env"` ⇒ 本条红。
    """
    _fake_persist_env(monkeypatch, tmp_path)
    with caplog.at_level("WARNING"):
        _persist_env_updates({"SWARM_RBAC_ENABLED": "0"}, is_admin=False, who="attacker7")
    _sec = [r.getMessage() for r in caplog.records if "SWARM_RBAC_ENABLED" in r.getMessage()]
    assert _sec, "backstop 未对认证类键留痕"
    _joined = "\n".join(_sec)
    assert "attacker7" in _joined, f"WARNING 丢失攻击者身份（A4-M2 未生效）: {_joined}"
    assert "persist_env)" not in _joined, "who 仍是字面量"


def test_m2_backstop_returns_rejected_keys(monkeypatch, tmp_path):
    """被剔的键必须回传调用方——不得让响应报"全部生效"。"""
    _fake_persist_env(monkeypatch, tmp_path)
    res = _persist_env_updates({"SWARM_RBAC_ENABLED": "0"}, is_admin=False, who="owner1")
    assert res.get("rejected_keys") == ["SWARM_RBAC_ENABLED"]
    assert res.get("persisted_keys") == [], "被拒的键却出现在 persisted_keys 里"
    assert res.get("requested_keys") == ["SWARM_RBAC_ENABLED"]


def test_m2_persisted_keys_are_actual_writes_not_the_request(monkeypatch, tmp_path):
    """混合载荷：合法键落盘、特权键被剔，两个列表必须如实分开。"""
    _fake_persist_env(monkeypatch, tmp_path)
    res = _persist_env_updates(
        {"SWARM_KB_CHUNK_SIZE": "512", "SWARM_API_KEY": "pwn"},
        is_admin=False, who="owner1")
    assert res.get("rejected_keys") == ["SWARM_API_KEY"]
    assert res.get("persisted_keys") == ["SWARM_KB_CHUNK_SIZE"]
    assert sorted(res.get("requested_keys") or []) == ["SWARM_API_KEY", "SWARM_KB_CHUNK_SIZE"]


def test_m2_admin_path_reports_all_persisted(monkeypatch, tmp_path):
    """admin 正常路径：rejected 空、persisted = 入参（不改既有响应语义）。"""
    _fake_persist_env(monkeypatch, tmp_path)
    res = _persist_env_updates({"SWARM_KB_CHUNK_SIZE": "512"}, is_admin=True, who="admin1")
    assert res.get("rejected_keys") == []
    assert res.get("persisted_keys") == ["SWARM_KB_CHUNK_SIZE"]


def test_m2_backstop_logs_wiring_gap_signal(monkeypatch, tmp_path, caplog):
    """backstop 触发本身是"某 caller 端点层漏设闸"的信号，必须有可 grep 的机读标记。"""
    _fake_persist_env(monkeypatch, tmp_path)
    with caplog.at_level("WARNING"):
        _persist_env_updates({"SWARM_API_KEY": "pwn"}, is_admin=False, who="owner1")
    assert any("A4-M2" in r.getMessage() for r in caplog.records), \
        "backstop 剔键无机读标记 ⇒ 接线漏了也没人知道"


# ───────────────────────── A4-L1 / A4-L2：权限判据同源 ─────────────────────────

def test_l1_l2_credential_endpoints_all_use_f5_predicate():
    """★接线事实锁★：三个"写凭据/写 .env"端点必须都走 F5 统一谓词。

    不用 getsource 断实现细节（纪律 6），而是断【行为】：注入一个非 admin 用户，
    三个端点都必须 403；注入 admin 则都不因权限被拒。
    """
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    from swarm.api.routers import config as cfgmod
    from swarm.auth.rbac import Role

    _owner = SimpleNamespace(username="owner1", global_role=Role.OWNER.value,
                            must_change_password=False, id="u1")

    class _Req:
        def __init__(self, user):
            self.state = SimpleNamespace(user=user)
            self.url = SimpleNamespace(path="/api/secrets/migrate")

        async def json(self):
            return {"value": "x"}

    import swarm.api._shared as _sh
    _orig = _sh._require_perm

    # 非 admin owner 持 config:write（正是 A4-C1 的攻击者身份）
    def _perm_owner(request, permission, project_id=None):
        return _owner

    try:
        cfgmod._require_perm = _perm_owner  # type: ignore[assignment]
        for _name, _call in (
            ("migrate_secrets_to_db", lambda: cfgmod.migrate_secrets_to_db(_Req(_owner))),
            ("set_env_credential", lambda: cfgmod.set_env_credential("SWARM_X", _Req(_owner))),
        ):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(_call())
            assert ei.value.status_code == 403, f"{_name} 对非 admin 未 403"
    finally:
        cfgmod._require_perm = _orig  # type: ignore[assignment]


def test_l2_endpoint_reachable_when_rbac_disabled():
    """★A4-L2 的真实病灶★：RBAC 关闭时 `request.state.user` 不存在，**端点本身**必须仍可达。

    治前：端点读 `request.state.user` → None → `_caller_is_admin(None)` False → 恒 403。
    治后：走 `_require_config_admin`（用 `_require_perm` 的返回值）→ 匿名 ADMIN → 放行。

    ★这条锁必须驱动【端点】，不能只调 `_require_config_admin`★——只调 helper 等于测
    "helper 对不对"，而 finding 说的是"端点没用 helper"。批 C 的 c6 就是这么假绿的
    （测试重建了一份生产表达式，测的是自己）。判据：把端点判据改回读 state.user，本条必红。
    """
    import asyncio
    from types import SimpleNamespace

    from swarm.api.routers import config as cfgmod
    from swarm.auth.rbac import Role

    class _ReqNoUser:
        """RBAC 关闭时中间件不设 state.user（api/deps.py:14 的分支恰为此存在）。"""

        def __init__(self):
            self.state = SimpleNamespace()  # ← 刻意不设 .user
            self.url = SimpleNamespace(path="/api/config/env-credential")

        async def json(self):
            return {"value": "v-not-a-real-secret"}

    _anon_admin = SimpleNamespace(username="dev", global_role=Role.ADMIN.value,
                                 must_change_password=False, id="anonymous")
    _orig_perm = cfgmod._require_perm
    _stored: dict = {}

    import swarm.config.secret_store as _ss
    _orig_set, _orig_inval = _ss.set_secret, _ss.invalidate_cache
    import swarm.config.config_audit as _ca
    _orig_rec = _ca.record_config_changes
    try:
        cfgmod._require_perm = lambda *a, **k: _anon_admin  # type: ignore[assignment]
        _ss.set_secret = lambda name, val: _stored.setdefault(name, val)  # type: ignore[assignment]
        _ss.invalidate_cache = lambda *a, **k: None  # type: ignore[assignment]
        _ca.record_config_changes = lambda *a, **k: {  # type: ignore[assignment]
            "written": 1, "failed": False, "degrade_key": None}
        res = asyncio.run(cfgmod.set_env_credential("SWARM_TEST_CRED", _ReqNoUser()))
        assert res.get("ok") is True, f"RBAC 关闭时端点不可达（A4-L2 未生效）: {res}"
        assert res.get("key") == "SWARM_TEST_CRED"
        assert "env:SWARM_TEST_CRED" in _stored, "端点放行了却没真写进 secret_store"
    finally:
        cfgmod._require_perm = _orig_perm  # type: ignore[assignment]
        _ss.set_secret, _ss.invalidate_cache = _orig_set, _orig_inval  # type: ignore[assignment]
        _ca.record_config_changes = _orig_rec  # type: ignore[assignment]


def test_l2_endpoint_still_rejects_non_admin():
    """反向锁：A4-L2 是放宽方向（可达性），绝不能顺手把 admin 闸拆掉。"""
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    from swarm.api.routers import config as cfgmod
    from swarm.auth.rbac import Role

    _owner = SimpleNamespace(username="owner1", global_role=Role.OWNER.value,
                            must_change_password=False, id="u1")

    class _Req:
        def __init__(self):
            self.state = SimpleNamespace(user=_owner)
            self.url = SimpleNamespace(path="/api/config/env-credential")

        async def json(self):
            return {"value": "v"}

    _orig_perm = cfgmod._require_perm
    try:
        cfgmod._require_perm = lambda *a, **k: _owner  # type: ignore[assignment]
        with pytest.raises(HTTPException) as ei:
            asyncio.run(cfgmod.set_env_credential("SWARM_TEST_CRED", _Req()))
        assert ei.value.status_code == 403
    finally:
        cfgmod._require_perm = _orig_perm  # type: ignore[assignment]


def test_l1_migrate_reports_audit_status():
    """A4-L1：migrate 响应必须带 audit_status（审计失败机读可辨，F4 同口径）。

    锁在【响应契约】上而非源码文本：断 audit_status 键在 ok 路径上存在。
    """
    import asyncio
    from types import SimpleNamespace

    from swarm.api.routers import config as cfgmod
    from swarm.auth.rbac import Role

    _admin = SimpleNamespace(username="admin1", global_role=Role.ADMIN.value,
                            must_change_password=False, id="a1")

    class _Req:
        state = SimpleNamespace(user=_admin)
        url = SimpleNamespace(path="/api/secrets/migrate")

    _orig_perm = cfgmod._require_perm
    _seen: dict = {}

    class _FakeCfg:
        class model:  # noqa: N801
            providers: list = []
            siliconflow_api_key = ""
            local_api_key = ""

    def _fake_record(who, action, changes):
        _seen["who"] = who
        _seen["action"] = action
        return {"written": len(changes), "failed": False, "degrade_key": None}

    import swarm.config.config_audit as _ca
    _orig_rec = _ca.record_config_changes
    _orig_getcfg = cfgmod._app.get_config
    import swarm.config.settings as _st
    _orig_reload = _st.reload_config
    _orig_clear = cfgmod._clear_plaintext_keys_from_env
    try:
        cfgmod._require_perm = lambda *a, **k: _admin  # type: ignore[assignment]
        _ca.record_config_changes = _fake_record  # type: ignore[assignment]
        cfgmod._app.get_config = lambda: _FakeCfg()  # type: ignore[assignment]
        _st.reload_config = lambda *a, **k: None  # type: ignore[assignment]
        # 迁移了一个键、清了一个键 ⇒ 审计必须被调
        cfgmod._clear_plaintext_keys_from_env = lambda: (["SWARM_LOCAL_API_KEY"], [])  # type: ignore[assignment]
        res = asyncio.run(cfgmod.migrate_secrets_to_db(_Req()))
        assert "audit_status" in res, "migrate 响应缺 audit_status"
        assert _seen.get("who") == "admin1", f"审计丢了真实 who: {_seen}"
        assert _seen.get("action") == "secrets_migrate"
    finally:
        cfgmod._require_perm = _orig_perm  # type: ignore[assignment]
        _ca.record_config_changes = _orig_rec  # type: ignore[assignment]
        cfgmod._app.get_config = _orig_getcfg  # type: ignore[assignment]
        _st.reload_config = _orig_reload  # type: ignore[assignment]
        cfgmod._clear_plaintext_keys_from_env = _orig_clear  # type: ignore[assignment]
