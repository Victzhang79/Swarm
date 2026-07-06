#!/usr/bin/env python3
"""P0-B：GET /api/health/ready 就绪探针单测。

修前：端点不存在（404）→ 容器 HEALTHCHECK 打 /api/health 只做存活、不探依赖 → 假绿。
修后：/api/health/ready 真实 ping 启用中的依赖（PG 必查；Redis 仅启用时；Qdrant 含本地文件兜底），
任一启用依赖不可达 → 503。/api/health 保持纯存活语义不变。

全部用 monkeypatch 桩掉三个探测函数，不依赖真 PG/Redis/Qdrant（CI 无服务）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _client():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    return TestClient(app)


# ── 探测桩（async，与真实探测签名一致：无参 → (ok, detail)） ─────────


async def _pg_ok():
    return True, "SELECT 1 ok"


async def _pg_fail():
    return False, "OperationalError"


async def _redis_ok():
    return True, "ping ok"


async def _redis_fail():
    return False, "unreachable"


async def _qdrant_ok():
    return True, "server online, 3 collections"


async def _qdrant_fail():
    return False, "unreachable"


# ── 用例 ─────────────────────────────────────────────


def test_ready_all_up_returns_200():
    with patch("swarm.api.app._probe_pg_ready", _pg_ok), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_ok), \
         patch("swarm.api.app.redis_enabled", return_value=False):
        resp = _client().get("/api/health/ready")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["postgres"]["ok"] is True
    assert body["checks"]["qdrant"]["ok"] is True
    # Redis 未启用 → 不计入失败，标 disabled
    assert body["checks"]["redis"]["ok"] is True
    assert body["checks"]["redis"]["detail"] == "disabled"
    print("  ✅ 全依赖 up → 200 ok")


def test_ready_pg_down_returns_503():
    with patch("swarm.api.app._probe_pg_ready", _pg_fail), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_ok), \
         patch("swarm.api.app.redis_enabled", return_value=False):
        resp = _client().get("/api/health/ready")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["postgres"]["ok"] is False
    print("  ✅ PG down → 503")


def test_ready_qdrant_down_returns_503():
    with patch("swarm.api.app._probe_pg_ready", _pg_ok), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_fail), \
         patch("swarm.api.app.redis_enabled", return_value=False):
        resp = _client().get("/api/health/ready")
    assert resp.status_code == 503, resp.text
    assert resp.json()["checks"]["qdrant"]["ok"] is False
    print("  ✅ Qdrant down → 503")


def test_ready_redis_disabled_not_a_failure():
    """Redis 默认关（SWARM_REDIS_ENABLED=false）→ 不因它 503。"""
    with patch("swarm.api.app._probe_pg_ready", _pg_ok), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_ok), \
         patch("swarm.api.app.redis_enabled", return_value=False):
        resp = _client().get("/api/health/ready")
    assert resp.status_code == 200, resp.text
    print("  ✅ Redis 未启用 → 不影响就绪")


def test_ready_redis_enabled_but_down_returns_503():
    """Redis 启用但不可达 → 计入失败 → 503（fail-closed，仅对启用依赖）。"""
    with patch("swarm.api.app._probe_pg_ready", _pg_ok), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_ok), \
         patch("swarm.api.app.redis_enabled", return_value=True), \
         patch("swarm.api.app._probe_redis_ready", _redis_fail):
        resp = _client().get("/api/health/ready")
    assert resp.status_code == 503, resp.text
    assert resp.json()["checks"]["redis"]["ok"] is False
    print("  ✅ Redis 启用+down → 503")


def test_ready_redis_enabled_up_returns_200():
    with patch("swarm.api.app._probe_pg_ready", _pg_ok), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_ok), \
         patch("swarm.api.app.redis_enabled", return_value=True), \
         patch("swarm.api.app._probe_redis_ready", _redis_ok):
        resp = _client().get("/api/health/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"]["redis"]["ok"] is True
    print("  ✅ Redis 启用+up → 200")


def test_ready_rbac_on_hides_topology():
    """F4：RBAC 开（生产）时，匿名 /ready 只回状态位，不泄露 per-component 拓扑（checks）。

    探针只需 200/503；组件明细收敛到需鉴权的 /api/status。RBAC 关（dev/CI）仍带 checks（上方用例）。
    """
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.rbac_enabled = True
    with patch("swarm.api.app._probe_pg_ready", _pg_ok), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_ok), \
         patch("swarm.api.app.redis_enabled", return_value=False), \
         patch("swarm.api.app.get_config", return_value=cfg):
        resp = _client().get("/api/health/ready")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert "checks" not in body, "RBAC 开启时匿名 /ready 不得泄露组件拓扑"
    # 503 路径同样只回状态位
    with patch("swarm.api.app._probe_pg_ready", _pg_fail), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_ok), \
         patch("swarm.api.app.redis_enabled", return_value=False), \
         patch("swarm.api.app.get_config", return_value=cfg):
        resp2 = _client().get("/api/health/ready")
    assert resp2.status_code == 503
    assert "checks" not in resp2.json()
    print("  ✅ RBAC 开→匿名 /ready 隐藏拓扑（200/503 均只回状态）")


def test_liveness_health_unchanged():
    """/api/health 仍是纯存活：200 + 不含依赖探测字段。"""
    resp = _client().get("/api/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert "checks" not in body  # 存活探针不做依赖检查
    print("  ✅ /api/health 纯存活语义不变")


def test_ready_no_secret_leak():
    """就绪探针公开可达 → 响应体不得回显连接串/密码（#21 信息泄漏防线）。"""
    from swarm.config.settings import get_config

    uri = get_config().db.postgres_uri
    with patch("swarm.api.app._probe_pg_ready", _pg_fail), \
         patch("swarm.api.app._probe_qdrant_ready", _qdrant_fail), \
         patch("swarm.api.app.redis_enabled", return_value=False):
        resp = _client().get("/api/health/ready")
    text = resp.text
    assert uri not in text
    for frag in ("password", "swarm:swarm", "@localhost", "postgresql://"):
        assert frag not in text, f"泄漏片段: {frag}"
    print("  ✅ 无密钥/连接串泄漏")


# ── Finding-1（对抗复核）：server 模式 Qdrant 不得用陈旧本地文件误判假绿 ──


class _FakeAsyncClient:
    """httpx.AsyncClient 桩：get() 抛连接错误，模拟服务器不可达。"""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        raise ConnectionError("qdrant down")


class _Cfg:
    def __init__(self, url):
        self.db = type("D", (), {"qdrant_url": url})()


def _run_qdrant_probe(qdrant_url: str):
    import asyncio
    import importlib

    import httpx

    app_mod = importlib.import_module("swarm.api.app")

    with patch.object(app_mod, "get_config", lambda: _Cfg(qdrant_url)), \
         patch.object(httpx, "AsyncClient", _FakeAsyncClient), \
         patch.object(app_mod.os.path, "exists", lambda p: True):  # 宿主机上有陈旧 ~/.swarm/qdrant
        return asyncio.run(app_mod._probe_qdrant_ready())


def test_is_local_qdrant():
    from swarm.api.app import _is_local_qdrant

    assert _is_local_qdrant("http://localhost:6333")
    assert _is_local_qdrant("http://127.0.0.1:6333")
    assert not _is_local_qdrant("http://qdrant-svc:6333")
    assert not _is_local_qdrant("http://qdrant.prod.internal:6333")
    print("  ✅ _is_local_qdrant 环回判定")


def test_qdrant_server_mode_down_not_false_green():
    """server 模式（远端 URL）+ 服务器不可达 + 宿主机有陈旧本地文件 → 必须判 unreachable（非假绿）。"""
    ok, detail = _run_qdrant_probe("http://qdrant-svc:6333")
    assert ok is False, f"server 模式不可达却假绿: {detail}"
    assert detail == "unreachable"
    print("  ✅ server 模式不可达不误判 local file")


def test_qdrant_local_mode_down_allows_file_fallback():
    """本地模式（localhost）+ 服务器不可达 + 有本地文件 → 允许 local file mode 兜底。"""
    ok, detail = _run_qdrant_probe("http://localhost:6333")
    assert ok is True
    assert detail == "local file mode"
    print("  ✅ 本地模式保留文件兜底")


if __name__ == "__main__":
    test_is_local_qdrant()
    test_qdrant_server_mode_down_not_false_green()
    test_qdrant_local_mode_down_allows_file_fallback()
    test_ready_all_up_returns_200()
    test_ready_pg_down_returns_503()
    test_ready_qdrant_down_returns_503()
    test_ready_redis_disabled_not_a_failure()
    test_ready_redis_enabled_but_down_returns_503()
    test_ready_redis_enabled_up_returns_200()
    test_liveness_health_unchanged()
    test_ready_no_secret_leak()
    print("\n✅ P0-B readiness 全过")
