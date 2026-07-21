"""B8 API/认证/安全面深读治本（06_api_auth F1..F5 = #62-66）行为级测试。

覆盖：
- F1 /api/status 非 admin 掩内部坐标（_check_component detail 掩空）
- F2 出站端点键（*_BASE_URL 等）分类器（provider key 钓鱼面）
- F3 WS ?token= 弃用兜底打 deprecation WARNING（不再静默）
- F4 must_change_password 423 上提到 _require_user（含改密白名单防死锁）
- F5 登录限流键取真实 client IP（可信代理跳数，fail-closed 默认不信任 XFF）
"""
from __future__ import annotations

import asyncio
import logging
import types

import pytest

from swarm.auth.rbac import Role
from swarm.auth.store import SwarmUser


# ─────────────────────────── F1 /api/status 非 admin 掩码 ───────────────────────────

def test_f1_check_component_masks_detail_for_non_admin():
    """非 admin：detail 掩空（不泄漏 worker 主模型 / sandbox api_url / PG version 等坐标）。"""
    from swarm.api.app import _check_component
    res = asyncio.run(_check_component("Brain 状态机", is_admin=False))
    assert res["detail"] == "", "非 admin 必须掩空 detail"
    assert "status" in res, "健康红绿灯(status)仍保留"


def test_f1_check_component_keeps_detail_for_admin():
    """admin：detail 保留（运维需完整拓扑）。"""
    from swarm.api.app import _check_component
    res = asyncio.run(_check_component("Brain 状态机", is_admin=True))
    # 成功→'Graph compiled OK'；失败→异常串——两者皆非空。
    assert res["detail"] != "", "admin 应得完整 detail"


def test_f1_check_component_default_is_fail_closed():
    """对抗复核 MEDIUM：默认 is_admin=False（铁律#3 安全布尔缺省）——忘传参=掩码不泄露。"""
    import inspect

    from swarm.api.app import _check_component
    p = inspect.signature(_check_component).parameters["is_admin"]
    assert p.default is False, "is_admin 默认必须 fail-closed False"
    res = asyncio.run(_check_component("Brain 状态机"))  # 不传 → 默认掩码
    assert res["detail"] == ""


# ─────────────────────────── F2 出站端点键分类器 ───────────────────────────

def test_f2_endpoint_redirect_key_classifier():
    from swarm.api.routers.config import _is_endpoint_redirect_key
    # 出站端点/连接键（改指向攻击者 host = key/DB/webhook 钓鱼 / MITM）→ 需 admin
    # 对抗复核 HIGH：含裸 _URL/_URI（原只列 _BASE_URL/_API_URL 会漏）
    for k in (
        "SWARM_MODEL_SILICONFLOW_BASE_URL",
        "SWARM_MODEL_LOCAL_BASE_URL",
        "SWARM_SANDBOX_API_URL",
        "SWARM_SANDBOX_PROXY_BASE",
        "SWARM_LANGSMITH_ENDPOINT",
        "SWARM_GITLAB_URL",          # 裸 _URL（GitLab PAT 发往此 host）
        "SWARM_KB_RERANK_URL",       # 裸 _URL（rerank key 发往此 host）
        "SWARM_DB_POSTGRES_URI",     # 裸 _URI（DB 凭据）
        "SWARM_NOTIFY_WEBHOOK_URL",  # 裸 _URL（webhook token）
    ):
        assert _is_endpoint_redirect_key(k), f"{k} 应判为出站端点键"
    # 非端点键（模型名/开关/预算/凭据本身）→ 走原 config:write，不误拦
    for k in (
        "SWARM_MODEL_WORKER_PRIMARY",
        "SWARM_MODEL_SILICONFLOW_API_KEY",
        "SWARM_MODEL_TIER_ENABLED",
        "SWARM_EXTRACT_MAX_ITEMS",
        "SWARM_NOTIFY_CHANNELS",
    ):
        assert not _is_endpoint_redirect_key(k), f"{k} 不应被端点闸误拦"


def test_f2_reject_endpoint_keys_chokepoint():
    """集中 chokepoint：非 admin 剔除端点键留普通键；admin 原样。对抗复核 CRITICAL 治本点。"""
    from swarm.api.routers.config import _reject_endpoint_keys
    m = {
        "SWARM_MODEL_SILICONFLOW_BASE_URL": "http://attacker/v1",  # 端点键
        "SWARM_MODEL_WORKER_PRIMARY": "some-model",               # 普通键
    }
    # 非 admin：端点键被剔除，普通键保留
    out = _reject_endpoint_keys(dict(m), is_admin=False, who="owner")
    assert "SWARM_MODEL_SILICONFLOW_BASE_URL" not in out, "非 admin 端点键必须剔除（钓鱼面）"
    assert out.get("SWARM_MODEL_WORKER_PRIMARY") == "some-model"
    # admin：全保留
    out2 = _reject_endpoint_keys(dict(m), is_admin=True, who="admin")
    assert out2 == m, "admin 原样放行"


def test_f2_persist_env_updates_requires_is_admin():
    """_persist_env_updates 的 is_admin 为必填 kwarg（backstop 强制每 caller 表态）。"""
    import inspect

    from swarm.api.routers import config as cfgmod
    sig = inspect.signature(cfgmod._persist_env_updates)
    p = sig.parameters.get("is_admin")
    assert p is not None and p.kind == inspect.Parameter.KEYWORD_ONLY, "is_admin 必须是 keyword-only"
    assert p.default is inspect.Parameter.empty, "is_admin 无默认值（必填，fail-closed）"


# ─────────────────────────── F3 WS ?token= 弃用告警 ───────────────────────────

def _fake_ws(*, query_token=None, headers=None, cookies=None):
    return types.SimpleNamespace(
        query_params={"token": query_token} if query_token else {},
        headers=headers or {},
        cookies=cookies or {},
    )


def test_f3_ws_query_token_still_works_but_warns(caplog):
    from swarm.api.auth import _extract_token_ws
    ws = _fake_ws(query_token="SECRET123")
    with caplog.at_level(logging.WARNING, logger="swarm.api.auth"):
        tok = _extract_token_ws(ws)
    assert tok == "SECRET123", "程序化客户端兜底仍可用（不打断非浏览器 WS）"
    assert any("?token=" in r.message or "弃用" in r.message for r in caplog.records), \
        "走 ?token= 必须打 deprecation WARNING（不再静默）"


def test_f3_ws_header_token_no_warning(caplog):
    from swarm.api.auth import _extract_token_ws
    ws = _fake_ws(headers={"x-swarm-token": "HDR"})
    with caplog.at_level(logging.WARNING, logger="swarm.api.auth"):
        tok = _extract_token_ws(ws)
    assert tok == "HDR"
    assert not caplog.records, "header 取值不应触发弃用告警"


# ─────────────────────────── F4 must_change_password 423 单一入口 ───────────────────────────

def _fake_request(user, path):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(user=user),
        url=types.SimpleNamespace(path=path),
    )


def _mc_user():
    return SwarmUser(
        id="u1", username="admin", display_name="A",
        global_role=Role.ADMIN.value, must_change_password=True,
    )


def test_f4_must_change_pw_blocks_non_whitelisted_path():
    from fastapi import HTTPException

    from swarm.api._shared import _require_user
    req = _fake_request(_mc_user(), "/api/models")
    with pytest.raises(HTTPException) as ei:
        _require_user(req)
    assert ei.value.status_code == 423


def test_f4_must_change_pw_allows_change_password_path():
    from swarm.api._shared import _require_user
    req = _fake_request(_mc_user(), "/api/auth/change-password")
    u = _require_user(req)  # 不抛
    assert u.username == "admin"


def test_f4_normal_user_unaffected():
    from swarm.api._shared import _require_user
    normal = SwarmUser(
        id="u2", username="dev", display_name="D",
        global_role=Role.DEVELOPER.value, must_change_password=False,
    )
    req = _fake_request(normal, "/api/models")
    assert _require_user(req).username == "dev"


# ─────────────────────────── F5 登录限流真实 client IP ───────────────────────────

class _MultiHeaders:
    """模拟 Starlette Headers：getlist 返回全部同名条目，get 只返回第一条（复现 hunter HIGH 场景）。"""
    def __init__(self, pairs):
        self._pairs = pairs  # list[(name, value)]

    def getlist(self, name):
        return [v for k, v in self._pairs if k.lower() == name.lower()]

    def get(self, name, default=""):
        for k, v in self._pairs:
            if k.lower() == name.lower():
                return v
        return default


def _ip_request(*, client_host="10.0.0.1", xff=None, header_pairs=None):
    if header_pairs is not None:
        headers = _MultiHeaders(header_pairs)
    else:
        headers = _MultiHeaders([("x-forwarded-for", xff)] if xff is not None else [])
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=client_host),
        headers=headers,
    )


def test_f5_default_no_trust_uses_client_host(monkeypatch):
    monkeypatch.delenv("SWARM_TRUSTED_PROXY_HOPS", raising=False)
    from swarm.api.routers.auth import _real_client_ip
    req = _ip_request(client_host="192.168.1.1", xff="1.2.3.4")
    assert _real_client_ip(req) == "192.168.1.1", "未配可信代理→XFF 不可信→退回 client.host"


def test_f5_one_trusted_hop_resolves_real_client(monkeypatch):
    monkeypatch.setenv("SWARM_TRUSTED_PROXY_HOPS", "1")
    from swarm.api.routers.auth import _real_client_ip
    # 单反代：XFF 只含真实 client；client.host=nginx
    req = _ip_request(client_host="10.0.0.9", xff="203.0.113.7")
    assert _real_client_ip(req) == "203.0.113.7"


def test_f5_spoofed_xff_prefix_ignored(monkeypatch):
    monkeypatch.setenv("SWARM_TRUSTED_PROXY_HOPS", "1")
    from swarm.api.routers.auth import _real_client_ip
    # 攻击者伪造 XFF 前缀；可信反代 append 攻击者真实 IP 在最右 → 取最右（=len-hops）忽略伪造
    req = _ip_request(client_host="10.0.0.9", xff="9.9.9.9, 203.0.113.7")
    assert _real_client_ip(req) == "203.0.113.7", "伪造的 XFF 前缀必须被忽略"


def test_f5_short_chain_fails_closed(monkeypatch):
    monkeypatch.setenv("SWARM_TRUSTED_PROXY_HOPS", "2")
    from swarm.api.routers.auth import _real_client_ip
    # 配 2 跳但 XFF 只有 1 条（异常/绕过）→ fail-closed 退回直连 peer
    req = _ip_request(client_host="10.0.0.9", xff="203.0.113.7")
    assert _real_client_ip(req) == "10.0.0.9"


def test_f5_multiple_xff_header_lines_merged(monkeypatch):
    """对抗复核 HIGH：反代以【追加独立 header 行】转发时，必须合并全部同名行按 RFC 解析——
    不能只取攻击者自发的第一条。攻击者发 XFF: 9.9.9.9，可信代理追加独立行 XFF: 真实IP。"""
    monkeypatch.setenv("SWARM_TRUSTED_PROXY_HOPS", "1")
    from swarm.api.routers.auth import _real_client_ip
    req = _ip_request(client_host="10.0.0.9", header_pairs=[
        ("x-forwarded-for", "9.9.9.9"),          # 攻击者伪造的第一条
        ("x-forwarded-for", "203.0.113.7"),      # 可信代理追加的独立行（真实）
    ])
    # 合并成 "9.9.9.9, 203.0.113.7"，hops=1 取最右 → 真实 IP，忽略伪造
    assert _real_client_ip(req) == "203.0.113.7", "多 header 行必须合并，伪造首行不得胜出"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
