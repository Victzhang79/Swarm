"""C8/C9（round22, P2）：uploads 存储滥用 + CLI demo 绕 RBAC。

C8：/api/uploads task:create 不绑项目 → 全局 developer 反复上传占存储。治本（务实）：加
    per-user rate_limit 限流（+ 既有 60MB/批上限）bound 滥用；per-project 配额需 DB 追踪，后延。
C9：swarm demo 本地 Brain 直跑绕过 API/RBAC/审计 → env 开关门控（默认禁用）。
"""
from __future__ import annotations

import importlib

appmod = importlib.import_module("swarm.api.app")
app = appmod.app


def _uses_rate_limit(call):
    return call is not None and (
        getattr(call, "__module__", "") == "swarm.api.rate_limit"
        or "rate_limit" in getattr(call, "__qualname__", "")
    )


def _collect_dep_calls(dependant):
    calls = []
    for d in getattr(dependant, "dependencies", []) or []:
        calls.append(getattr(d, "call", None))
        calls.extend(_collect_dep_calls(d))
    return calls


def _has_rate_limit(path, method):
    # CI 修正：查已解析的 dependant 树(跨 FastAPI 版本稳定) + raw route.dependencies。
    for r in app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set()):
            for d in getattr(r, "dependencies", []) or []:
                if _uses_rate_limit(getattr(d, "dependency", None)):
                    return True
            dep = getattr(r, "dependant", None)
            if dep and any(_uses_rate_limit(c) for c in _collect_dep_calls(dep)):
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
