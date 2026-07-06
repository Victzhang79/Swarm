"""P1-3（CODEWALK_AUDIT_2026-07-06 批2）：/docs /openapi.json /redoc 匿名公开。

原状：三端点在 _PUBLIC_PREFIXES 白名单——RBAC 开启的生产部署仍向匿名暴露全量 API
schema，与 #21"收 /api/status 需鉴权（基建信息泄露）"动机矛盾。
修：生产环境（is_production）docs 端点默认纳入鉴权（默认拒绝）；SWARM_DOCS_PUBLIC=true
显式放开；非生产保持公开（本地开发调试零摩擦）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import PlainTextResponse

import swarm.api.auth as auth_mod


class _Cfg:
    rbac_enabled = True

    def __init__(self, prod: bool):
        self._p = prod

    def is_production(self) -> bool:
        return self._p


def _req(path: str, headers: list | None = None) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path,
        "headers": headers or [], "query_string": b"",
        "scheme": "http", "server": ("test", 80), "client": ("test", 1),
    })


async def _ok(_request):
    return PlainTextResponse("ok")


def _dispatch(path: str, *, prod: bool, headers: list | None = None):
    mw = auth_mod.SwarmAuthMiddleware(app=None)
    with patch.object(auth_mod, "get_config", lambda: _Cfg(prod)):
        return asyncio.run(mw.dispatch(_req(path, headers), _ok))


def test_openapi_requires_auth_in_production(monkeypatch):
    monkeypatch.delenv("SWARM_DOCS_PUBLIC", raising=False)
    for p in ("/openapi.json", "/docs", "/redoc"):
        resp = _dispatch(p, prod=True)
        assert resp.status_code == 401, f"生产+RBAC 下匿名 {p} 应 401，实际 {resp.status_code}"


def test_docs_public_in_dev(monkeypatch):
    monkeypatch.delenv("SWARM_DOCS_PUBLIC", raising=False)
    for p in ("/openapi.json", "/docs", "/redoc"):
        resp = _dispatch(p, prod=False)
        assert resp.status_code == 200, f"非生产 {p} 应保持公开（开发调试）"


def test_docs_env_override_opens_in_production(monkeypatch):
    monkeypatch.setenv("SWARM_DOCS_PUBLIC", "true")
    resp = _dispatch("/openapi.json", prod=True)
    assert resp.status_code == 200, "SWARM_DOCS_PUBLIC=true 应显式放开"


def test_docs_with_valid_token_in_production(monkeypatch):
    monkeypatch.delenv("SWARM_DOCS_PUBLIC", raising=False)
    with patch.object(auth_mod, "resolve_user", lambda t: auth_mod._LEGACY_USER):
        resp = _dispatch("/docs", prod=True,
                         headers=[(b"x-swarm-token", b"tok-123")])
    assert resp.status_code == 200, "持有效 token 在生产下应可访问 docs（纳入鉴权非一刀切禁用）"


def test_docs_config_error_fails_closed(monkeypatch):
    """hunter #3：is_production() 判定抛异常不许把请求炸成 500（fail-undefined）——
    应 fail-closed 落入常规鉴权 → 匿名 401。"""
    monkeypatch.delenv("SWARM_DOCS_PUBLIC", raising=False)

    class _BoomCfg:
        rbac_enabled = True

        def is_production(self):
            raise RuntimeError("config store down")

    mw = auth_mod.SwarmAuthMiddleware(app=None)
    with patch.object(auth_mod, "get_config", lambda: _BoomCfg()):
        resp = asyncio.run(mw.dispatch(_req("/openapi.json"), _ok))
    assert resp.status_code == 401, f"配置异常应 fail-closed 401，实际 {resp.status_code}"


def test_non_docs_public_prefixes_unchanged(monkeypatch):
    monkeypatch.delenv("SWARM_DOCS_PUBLIC", raising=False)
    resp = _dispatch("/api/health", prod=True)
    assert resp.status_code == 200, "/api/health 存活探针必须保持匿名可达"
