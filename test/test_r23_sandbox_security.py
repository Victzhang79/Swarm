"""round23 审计治本 — R23-3 沙箱/本地边界。

(a) sync_files_from_sandbox 远端路径走 sandbox_path 归一化+containment（防 `..` 读 workspace 外）。
(b) L1 本地兜底 subprocess.run(shell=True) 前过命令黑名单（与 build_tools._run_local 对称）。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from swarm.worker import l1_pipeline
from swarm.worker.sandbox import sandbox_path


# ── (a) 路径 containment ──
def test_sandbox_path_rejects_traversal():
    with pytest.raises(ValueError):
        sandbox_path("../../etc/passwd")


def test_sandbox_path_normal_ok():
    assert sandbox_path("a/b.java").endswith("/workspace/a/b.java")


# ── (b) 本地兜底黑名单 ──
def test_l1_local_fallback_blocks_blacklisted():
    with patch("swarm.config.command_blacklist_store.check_command_hardened",
               return_value=(False, "danger pattern")):
        rc, out = l1_pipeline._run_l1_command("rm -rf /", "/tmp")
    assert rc == 126, (rc, out)
    assert "黑名单" in out


def test_l1_local_fallback_runs_allowed():
    with patch("swarm.config.command_blacklist_store.check_command_hardened",
               return_value=(True, "")):
        rc, out = l1_pipeline._run_l1_command("echo r23ok", "/tmp")
    assert rc == 0 and "r23ok" in out, (rc, out)


def test_l1_local_fallback_failclosed_on_blacklist_error():
    with patch("swarm.config.command_blacklist_store.check_command_hardened",
               side_effect=RuntimeError("store down")):
        rc, out = l1_pipeline._run_l1_command("echo x", "/tmp")
    assert rc == 126 and "fail-closed" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
