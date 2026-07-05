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
