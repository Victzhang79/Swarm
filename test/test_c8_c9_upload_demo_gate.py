"""C8/C9（round22, P2）：uploads 存储滥用 + CLI demo 绕 RBAC。

C8：/api/uploads task:create 不绑项目 → 全局 developer 反复上传占存储。治本（务实）：加
    per-user rate_limit 限流（+ 既有 60MB/批上限）bound 滥用；per-project 配额需 DB 追踪，后延。
C9：swarm demo 本地 Brain 直跑绕过 API/RBAC/审计 → env 开关门控（默认禁用）。
"""
from __future__ import annotations

import importlib
import inspect
import os


def test_uploads_rate_limit_wired_in_source():
    from swarm.api.routers import upload
    assert 'rate_limit("uploads"' in inspect.getsource(upload)


def test_uploads_rate_limited_behaviorally():
    """行为验证（跨版本鲁棒）：超容量后必 429，证明限流端到端生效（RBAC 关时 body 返 400）。

    共享 _limiter 桶为进程级——测试前后都清空，杜绝污染其它 /api/uploads 测试(顺序无关)。
    """
    os.environ.pop("SWARM_RATELIMIT_DISABLED", None)
    from fastapi.testclient import TestClient
    from swarm.api.rate_limit import _limiter
    with _limiter._lock:
        _limiter._buckets.clear()
    try:
        app = importlib.import_module("swarm.api.app").app
        client = TestClient(app)
        codes = [client.post("/api/uploads").status_code for _ in range(15)]
        assert 429 in codes, f"上传端点超限后应 429（限流未生效）: {codes}"
    finally:
        with _limiter._lock:
            _limiter._buckets.clear()  # 还原，不污染后续 upload 测试


def test_demo_disabled_by_default(monkeypatch):
    from swarm.cli import _demo_enabled
    monkeypatch.delenv("SWARM_DEMO_ENABLED", raising=False)
    assert _demo_enabled() is False


def test_demo_enabled_when_env_set(monkeypatch):
    from swarm.cli import _demo_enabled
    monkeypatch.setenv("SWARM_DEMO_ENABLED", "1")
    assert _demo_enabled() is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
