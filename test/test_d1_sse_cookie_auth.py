"""D1：SSE 凭据经 HttpOnly Cookie（避免 ?token= 进 URL 致跨用户泄漏）。

行为测试：_extract_token 的优先级(header > cookie；F1 起 ?token= 已关闭)，与
/api/auth/login 下发 HttpOnly Cookie。
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


def test_cookie_used_over_query_token():
    # F1：?token= 已关闭，即便同时给 query，也只认 Cookie。
    r = _Req(cookies={"swarm_token": "cookie-tok"}, query={"token": "qtok"})
    assert _extract_token(r) == "cookie-tok"


def test_query_token_no_longer_honored():
    # F1 治本：?token= URL 兜底已移除（进 access log/Referer/历史 = 跨用户凭据泄漏面）。
    # 只有 query token 而无 header/cookie → 视为无凭据（空串），中间件将判 401。
    assert _extract_token(_Req(query={"token": "qtok"})) == ""


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


def test_issue_token_cookie_single_source_of_truth():
    """D1 边界治本：登录与 /api/auth/me 引导共用的单一事实源 _issue_token_cookie 契约。

    /me 用它在 boot/自动登录路径续发 Cookie，使 SSE cookie 鉴权在浏览器重启丢失【会话
    Cookie】(ttl=0)后仍可用。这里直接锁 helper 的 Cookie 属性（不依赖 RBAC/登录态，确定性）。
    """
    from starlette.responses import Response as _Resp

    from swarm.api.routers.auth import _issue_token_cookie

    class _UrlHTTP:
        scheme = "http"

    class _ReqHTTP:
        url = _UrlHTTP()

    resp = _Resp()
    _issue_token_cookie(resp, _ReqHTTP(), "tok-abc")
    sc = resp.headers.get("set-cookie", "")
    low = sc.replace(" ", "").lower()
    assert "swarm_token=tok-abc" in sc, sc
    assert "httponly" in low, "Cookie 必须 HttpOnly（防 XSS 读取）"
    assert "path=/" in low, sc
    assert "samesite=lax" in low, "SameSite=Lax 允许同源 SSE GET"
    # 内网 HTTP（scheme=http）下不置 secure（否则 Cookie 不发）。
    assert "secure" not in low, "HTTP 下不应置 Secure（会致 Cookie 不发）"


def test_issue_token_cookie_sets_secure_on_https():
    """HTTPS 部署下应置 Secure。"""
    from starlette.responses import Response as _Resp

    from swarm.api.routers.auth import _issue_token_cookie

    class _UrlHTTPS:
        scheme = "https"

    class _ReqHTTPS:
        url = _UrlHTTPS()

    resp = _Resp()
    _issue_token_cookie(resp, _ReqHTTPS(), "tok-xyz")
    assert "secure" in resp.headers.get("set-cookie", "").lower()
