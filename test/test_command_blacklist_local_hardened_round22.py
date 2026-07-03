#!/usr/bin/env python3
"""P0-4 + #1(b) round22：命令黑名单覆盖本地执行 + 异常回退基线（不 fail-open）。

P0-4：黑名单唯一强制点在 sandbox.run_command（仅沙箱路径）；_run_local 直接 subprocess 无检查
→ 无沙箱时（get_sandbox_context 返 None）本地执行绕过黑名单纵深。
#1(b)：sandbox.py:833 `except: allowed=True` 检查抛异常即无条件放行 → import 失败/罕见异常
可让 `rm -rf /` 放行。

治本：check_command_hardened 异常回退内置基线（绝不无条件 True）；build_tools._run_local 上黑名单。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.config import command_blacklist_store as bl  # noqa: E402
from swarm.tools import build_tools  # noqa: E402


# ── #1(b)：hardened 检查异常回退基线，不 fail-open ──

def test_hardened_falls_back_to_baseline_on_error():
    # 模拟 check_command 抛异常（如 _enabled_patterns import/DB 罕见异常）
    with patch.object(bl, "check_command", side_effect=RuntimeError("boom")):
        allowed, reason = bl.check_command_hardened("rm -rf /")
    assert allowed is False, "异常回退基线必须仍拦住 rm -rf /（复现 fail-open bug）"
    assert reason
    print("  ✅ #1(b) 异常回退基线拦截 rm -rf /")


def test_hardened_allows_benign_on_error():
    with patch.object(bl, "check_command", side_effect=RuntimeError("boom")):
        allowed, _ = bl.check_command_hardened("mvn -q compile")
    assert allowed is True, "基线不该误伤正常 build 命令"
    print("  ✅ #1(b) 异常回退基线放行正常命令")


def test_hardened_normal_path():
    # 正常路径委托 check_command（不回退）
    allowed, _ = bl.check_command_hardened("echo hi")
    assert allowed is True
    print("  ✅ hardened 正常路径委托 check_command")


# ── P0-4：_run_local 经过黑名单（无沙箱本地执行也拦） ──

def test_run_local_blocks_blacklisted():
    build_tools.clear_sandbox_context()  # 确保走本地分支（get_sandbox_context 返 None）
    out = build_tools._run("rm -rf / --no-preserve-root")
    assert "黑名单" in out or "拦截" in out, f"本地危险命令应被黑名单拦截、绝不执行；得到 {out[:80]}"
    print("  ✅ P0-4 _run 本地分支拦截黑名单命令")


def test_run_local_allows_benign():
    import tempfile
    build_tools.clear_sandbox_context()
    # 显式传一个存在的 cwd —— 默认 _workspace_root()(=PROJECT_ROOT/workspace) 在 CI runner 上
    # 不存在会令 subprocess FileNotFoundError（与黑名单无关，纯环境）。
    out = build_tools._run("echo hello_round22", cwd=tempfile.mkdtemp())
    assert "hello_round22" in out, f"正常命令应正常执行；得到 {out[:80]}"
    print("  ✅ P0-4 本地正常命令不受影响")


if __name__ == "__main__":
    test_hardened_falls_back_to_baseline_on_error()
    test_hardened_allows_benign_on_error()
    test_hardened_normal_path()
    test_run_local_blocks_blacklisted()
    test_run_local_allows_benign()
    print("\n✅ P0-4 + #1(b) 全部通过")
