"""C8/C9（round22, P2）：uploads 存储滥用 + CLI demo 绕 RBAC。

C8：/api/uploads task:create 不绑项目 → 全局 developer 反复上传占存储。治本（务实）：加
    per-user rate_limit 限流（+ 既有 60MB/批上限）bound 滥用；per-project 配额需 DB 追踪，后延。
C9：swarm demo 本地 Brain 直跑绕过 API/RBAC/审计 → env 开关门控（默认禁用）。
"""
from __future__ import annotations

import importlib

appmod = importlib.import_module("swarm.api.app")
app = appmod.app


def _has_rate_limit(path, method):
    for r in app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set()):
            for d in getattr(r, "dependencies", []) or []:
                dep = getattr(d, "dependency", None)
                if getattr(dep, "__name__", "") == "_dep" and "rate_limit" in getattr(dep, "__qualname__", ""):
                    return True
    return False


def test_uploads_rate_limited():
    assert _has_rate_limit("/api/uploads", "POST")


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
