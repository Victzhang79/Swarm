"""A2 批2 单测：跨项目清理扩展 + per-project 隔离开关。

- _bucket_key：isolate_per_project 关时只按 template；开时按 project+template 分桶。
- clean_workspace 命令：含 /tmp 与 $HOME 缓存清理（防跨项目泄漏），保留 shell 配置。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from swarm.worker.sandbox_pool import HotSandboxPool


def _pool():
    return HotSandboxPool(MagicMock())


def test_bucket_key_default_template_only(monkeypatch):
    """默认 isolate_per_project=false → 桶键只含 template（高复用）。"""
    monkeypatch.delenv("SWARM_SANDBOX_ISOLATE_PER_PROJECT", raising=False)
    from swarm.config.settings import reload_config
    reload_config()
    p = _pool()
    assert p._bucket_key("tpl-x", "projA") == "tpl-x"
    assert p._bucket_key("tpl-x", "projB") == "tpl-x"  # 不同项目同桶 → 可复用


def test_bucket_key_isolated_per_project(monkeypatch):
    """isolate_per_project=true → 桶键含 project，跨项目不同桶（不复用）。"""
    monkeypatch.setenv("SWARM_SANDBOX_ISOLATE_PER_PROJECT", "true")
    from swarm.config.settings import reload_config
    reload_config()
    try:
        p = _pool()
        ka = p._bucket_key("tpl-x", "projA")
        kb = p._bucket_key("tpl-x", "projB")
        assert ka != kb, "不同项目应分到不同桶（隔离）"
        assert "projA" in ka and "projB" in kb
        # 无 project_id 时退回 template（如手动创建）
        assert p._bucket_key("tpl-x", None) == "tpl-x"
    finally:
        monkeypatch.delenv("SWARM_SANDBOX_ISOLATE_PER_PROJECT", raising=False)
        reload_config()


def test_clean_workspace_command_covers_tmp_home():
    """clean_workspace 下发的命令应覆盖 /tmp 与 $HOME 缓存，且保留 shell 配置。"""
    from swarm.worker.sandbox import SandboxManager
    captured = {}

    mgr = SandboxManager.__new__(SandboxManager)  # 不走 __init__（避免连真服务）

    def fake_run_command(sandbox, cmd, timeout=30, _skip_blacklist=False):
        captured["cmd"] = cmd
        return MagicMock(success=True, stdout="WORKSPACE_CLEANED", error=None)

    mgr.run_command = fake_run_command
    sb = MagicMock(sandbox_id="sid1")
    ok = mgr.clean_workspace(sb)
    assert ok is True
    cmd = captured["cmd"]
    assert "/workspace" in cmd
    assert "/tmp" in cmd
    assert "$HOME" in cmd
    assert ".cache" in cmd and ".npm" in cmd  # 缓存目录
    # 不应删除 shell 配置
    assert ".bashrc" not in cmd


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
