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


# ── (b') _run_check_split 本地兜底黑名单（与 _run_l1_command 对称补齐）──
def test_check_split_local_fallback_blocks_blacklisted():
    with patch("swarm.config.command_blacklist_store.check_command_hardened",
               return_value=(False, "danger pattern")):
        rc, out, err = l1_pipeline._run_check_split("rm -rf /", "/tmp")
    assert rc == 126, (rc, out, err)
    assert "黑名单" in err
    # fail-closed：拦截即不执行 → 无正常命令输出
    assert out == ""


def test_check_split_local_fallback_runs_allowed():
    with patch("swarm.config.command_blacklist_store.check_command_hardened",
               return_value=(True, "")):
        rc, out, err = l1_pipeline._run_check_split("echo r23splitok", "/tmp")
    assert rc == 0 and "r23splitok" in out, (rc, out, err)


def test_check_split_local_fallback_failclosed_on_blacklist_error():
    with patch("swarm.config.command_blacklist_store.check_command_hardened",
               side_effect=RuntimeError("store down")):
        rc, out, err = l1_pipeline._run_check_split("echo x", "/tmp")
    assert rc == 126 and "fail-closed" in err, (rc, out, err)


def test_check_split_checks_normalized_command_string():
    """校验对象=真正传给 shell 的命令串(normalize 之后)，杜绝 check/run 口径漂移。"""
    seen: dict[str, str] = {}

    def _spy(cmd):
        seen["cmd"] = cmd
        return (True, "")

    # 用 python 前缀命令，normalize_python_cmd 会把 python -> 实际解释器路径
    with patch("swarm.config.command_blacklist_store.check_command_hardened",
               side_effect=_spy):
        l1_pipeline._run_check_split("python --version", "/tmp")
    expected = l1_pipeline.normalize_python_cmd(
        "python --version", py_bin=l1_pipeline._python_bin())
    assert seen.get("cmd") == expected, seen


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
