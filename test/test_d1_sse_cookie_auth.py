"""D1：SSE 凭据经 HttpOnly Cookie（避免 ?token= 进 URL 致跨用户泄漏）。

行为测试：_extract_token 的优先级(header > cookie > ?token=)，与 /api/auth/login 下发
HttpOnly Cookie。
"""
from __future__ import annotations

import pytest

from swarm.api.auth import _extract_token


class _Req:
    def __init__(self, headers=None, cookies=None, query=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}


def test_extract_reads_cookie_when_no_header():
    assert _extract_token(_Req(cookies={"swarm_token": "cookie-tok"})) == "cookie-tok"


def test_header_beats_cookie():
    r = _Req(headers={"X-Swarm-Token": "hdr"}, cookies={"swarm_token": "cookie-tok"})
    assert _extract_token(r) == "hdr"


def test_cookie_beats_query_token():
    # Cookie(HttpOnly,不进 URL)优先于遗留 ?token=(进 access log/历史)
    r = _Req(cookies={"swarm_token": "cookie-tok"}, query={"token": "qtok"})
    assert _extract_token(r) == "cookie-tok"


def test_query_token_still_fallback():
    assert _extract_token(_Req(query={"token": "qtok"})) == "qtok"


def test_bearer_header_beats_all():
    r = _Req(headers={"Authorization": "Bearer btok"}, cookies={"swarm_token": "c"}, query={"token": "q"})
    assert _extract_token(r) == "btok"


def test_login_sets_httponly_cookie():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    client = TestClient(app)
    r = client.post("/api/auth/login", json={"username": "admin", "password": "swarm"})
    if r.status_code != 200:
        pytest.skip(f"默认 admin 登录不可用（RBAC/账户配置）: {r.status_code}")
    set_cookie = r.headers.get("set-cookie", "")
    assert "swarm_token=" in set_cookie, set_cookie
    assert "HttpOnly" in set_cookie, "Cookie 必须 HttpOnly（防 JS/XSS 读取）"
    assert "Path=/" in set_cookie


def test_logout_clears_httponly_cookie():
    """D1 治本：logout 端点必须 delete_cookie 清 swarm_token，否则伪退出（Cookie 存活→仍鉴权）。

    RBAC 无关的强断言：①响应含清除 swarm_token 的 Set-Cookie（Max-Age=0/过期）；
    ②浏览器语义下 client cookie jar 据此丢弃 swarm_token（证明浏览器不会再带该凭据）。
    """
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    client = TestClient(app)
    r = client.post("/api/auth/login", json={"username": "admin", "password": "swarm"})
    if r.status_code != 200:
        pytest.skip(f"默认 admin 登录不可用（RBAC/账户配置）: {r.status_code}")
    assert client.cookies.get("swarm_token"), "登录后 client cookie jar 应持有 swarm_token"

    lr = client.post("/api/auth/logout")
    assert lr.status_code == 200, lr.text
    sc = lr.headers.get("set-cookie", "")
    assert "swarm_token=" in sc, f"logout 应下发清除 swarm_token 的 Set-Cookie: {sc}"
    assert ("max-age=0" in sc.lower()) or ("expires=" in sc.lower()), f"应过期/清零: {sc}"
    # 浏览器 cookie jar 语义：logout 后不应再持有 swarm_token（否则同源请求仍会带 → 伪退出）
    assert not client.cookies.get("swarm_token"), "logout 后 cookie jar 仍有 swarm_token（伪退出未治）"


def test_logout_is_idempotent_without_session():
    """未登录/无 Cookie 直接 logout 也应恒成功（幂等清除），不 401。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    client = TestClient(app)
    lr = client.post("/api/auth/logout")
    assert lr.status_code == 200, lr.text
