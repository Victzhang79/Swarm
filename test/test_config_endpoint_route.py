#!/usr/bin/env python3
"""P0 回归锁：GET /api/config 必须绑定到 get_config_endpoint（而非辅助函数）。

背景：P1-20 曾把辅助函数 _is_local_or_private_host 插在 @router.get("/api/config")
装饰器与 get_config_endpoint 之间，导致装饰器作用到辅助函数上——真正的端点未注册、
且顶替函数无 _require_user 鉴权、还要求 ?url= 查询参数。此测试走路由级锁定，防复发。

注：introspection 走【config 路由模块自身的 router.routes】（装饰器绑定的源头，单模块、
不受其它测试对全局 app.routes 的状态污染影响）——比 app.routes 更稳（CI 上 app.routes
曾被其它测试污染导致 flaky）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _config_get_route():
    """在 config 路由模块自身的 router 上找 GET /api/config 路由对象（源头，稳定）。"""
    from swarm.api.routers import config as _cfg

    for r in _cfg.router.routes:
        if getattr(r, "path", None) == "/api/config" and "GET" in getattr(r, "methods", set()):
            return r
    return None


def test_api_config_route_bound_to_get_config_endpoint():
    """/api/config GET 端点必须是 get_config_endpoint，不能是任何 _ 辅助函数。"""
    route = _config_get_route()
    assert route is not None, "GET /api/config 路由未注册"
    assert route.endpoint.__name__ == "get_config_endpoint", (
        f"/api/config 被绑定到了错误的函数: {route.endpoint.__name__}"
    )
    # 不应要求 url 查询参数（旧回归会因辅助函数签名 url:str 要求它 → 422）。
    query_names = {q.name for q in route.dependant.query_params}
    assert "url" not in query_names, f"/api/config 不应有 url 查询参数: {query_names}"


def test_api_config_returns_config_not_helper_bool():
    """RBAC 关（dev 默认）GET /api/config → 200 且返回配置 dict（非布尔、无需 url）。

    旧回归绑定的辅助函数签名 url:str → 无 url 时 422；此测试功能性证明真实端点已恢复。
    """
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    client = TestClient(app)
    resp = client.get("/api/config")
    assert resp.status_code == 200, f"预期 200，实际 {resp.status_code}: {resp.text[:200]}"
    body = resp.json()
    assert isinstance(body, dict) and "config" in body, f"应返回配置 dict，实际: {body}"


def test_api_config_bound_endpoint_enforces_auth():
    """直接调用【路由实际绑定】的端点：RBAC 开 + 无 user → 抛 401。

    行为级锁（非源码检查）：既证明绑定的是带 _require_user 的端点，也证明鉴权未被绕过。
    旧回归绑定的是同步辅助函数(签名 url:str)，此调用会失败而非抛 401。
    """
    import asyncio

    import pytest
    from fastapi import HTTPException

    route = _config_get_route()
    assert route is not None

    req = MagicMock()
    req.state.user = None
    cfg = MagicMock()
    cfg.rbac_enabled = True
    with patch("swarm.api.deps.get_config", return_value=cfg):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(route.endpoint(req))
    assert ei.value.status_code == 401


def test_local_or_private_host_helper_still_importable():
    """辅助函数逻辑保持可用（上移后仍可 import 且行为不变）。"""
    from swarm.api.routers.config import _is_local_or_private_host as f

    assert f("http://localhost:8080") is True
    assert f("http://127.0.0.1:1234") is True
    assert f("http://10.0.0.5") is True
    assert f("https://api.openai.com") is False
    assert f("not a url") is False
