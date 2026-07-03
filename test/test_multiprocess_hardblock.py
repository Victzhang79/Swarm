#!/usr/bin/env python3
"""顺手项：_warn_if_multiprocess 由告警改【硬拦】(fail-fast) 单测。

修前：WEB_CONCURRENCY>1 仅打 warning、继续启动 → 多 worker 下 SSE/调度/队列 meta 静默错乱。
修后：启动期 raise RuntimeError 拒绝；逃生阀 SWARM_ALLOW_MULTIPROCESS=1 降级为告警。

纯逻辑，不依赖 DB。
"""

from __future__ import annotations

import importlib

import pytest

app_mod = importlib.import_module("swarm.api.app")


def test_single_worker_ok(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    app_mod._warn_if_multiprocess()  # 不抛


def test_unset_ok(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    app_mod._warn_if_multiprocess()  # 不抛


def test_multi_worker_hard_blocks(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.delenv("SWARM_ALLOW_MULTIPROCESS", raising=False)
    with pytest.raises(RuntimeError, match="多 worker"):
        app_mod._warn_if_multiprocess()


def test_override_downgrades_to_warning(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("SWARM_ALLOW_MULTIPROCESS", "1")
    app_mod._warn_if_multiprocess()  # 逃生阀 → 不抛


def test_malformed_web_concurrency_is_safe(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "notanumber")
    app_mod._warn_if_multiprocess()  # 解析失败按 1 处理，不抛


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
