#!/usr/bin/env python3
"""P0-1 round22：沙箱 git rc 判据鲁棒性（复现 git_tools.py:31 恒判失败）。

根因：`_run_git` 沙箱分支 `rc = 0 if "sandbox exit code 0" in raw else 1` 只认旧
Jupyter 兜底格式 `"✅ (sandbox exit code 0)"`；但 `_run_in_sandbox` 主路径(run_command)
成功返回 `"✅ (sandbox 0)\n..."`（不含 "sandbox exit code 0"）→ 沙箱主路径下 git
成功也判 rc=1，degrade 所有 git_diff/git_log/git_blame/git_checkout。

治本：判据改为对两种成功格式 + infra-fail 都鲁棒（首行 ✅ 前缀）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.tools import git_tools  # noqa: E402
from swarm.tools import build_tools  # noqa: E402


# _run_in_sandbox 的四种真实返回格式（见 build_tools.py:192/202/218/232）
MAIN_OK = "✅ (sandbox 0)\ndiff --git a/x b/x\n+foo"          # 主路径成功
MAIN_FAIL = "❌ (sandbox exit_code=1)\nerror: something"        # 主路径失败
JUPYTER_OK = "✅ (sandbox exit code 0)\ndiff --git a/x b/x"     # 旧兜底成功
INFRA_FAIL = "❌ 沙箱不可用(基础设施失败)，命令未执行(fail-closed 隔离边界): 502"  # infra-fail


def _call_run_git(raw: str):
    """在沙箱上下文下调用 _run_git，mock _run_in_sandbox 返回给定格式串。"""
    with patch.object(build_tools, "get_sandbox_context", return_value=(object(), object())), \
         patch.object(build_tools, "_run_in_sandbox", return_value=raw):
        return git_tools._run_git(["diff", "HEAD"])


def test_main_path_success_rc0():
    rc, out = _call_run_git(MAIN_OK)
    assert rc == 0, f"主路径成功必须 rc=0（复现 bug：当前恒 1）；raw={MAIN_OK!r}"
    assert "diff --git" in out
    print("  ✅ 主路径成功 (sandbox 0) → rc=0")


def test_jupyter_fallback_success_rc0():
    rc, _ = _call_run_git(JUPYTER_OK)
    assert rc == 0, "旧 Jupyter 兜底成功也应 rc=0（不回归）"
    print("  ✅ 旧兜底 (sandbox exit code 0) → rc=0")


def test_main_path_failure_rc1():
    rc, _ = _call_run_git(MAIN_FAIL)
    assert rc == 1, "主路径失败必须 rc=1"
    print("  ✅ 主路径失败 ❌ → rc=1")


def test_infra_fail_rc1():
    rc, _ = _call_run_git(INFRA_FAIL)
    assert rc == 1, "infra-fail 必须 rc=1（fail-closed，不当成功）"
    print("  ✅ infra-fail ❌ → rc=1")


def test_git_diff_tool_returns_body_not_error_on_main_success():
    """端到端：git_diff 工具在主路径成功串下应返回 body，而非 ❌ 失败字符串。"""
    with patch.object(build_tools, "get_sandbox_context", return_value=(object(), object())), \
         patch.object(build_tools, "_run_in_sandbox", return_value=MAIN_OK):
        result = git_tools.git_diff.func()  # unwrap @tool
    assert not result.startswith("❌"), f"主路径成功不该返回失败串；得到：{result!r}"
    assert "diff --git" in result or "foo" in result
    print("  ✅ git_diff 主路径成功 → 返回 body 非 ❌")


if __name__ == "__main__":
    test_main_path_success_rc0()
    test_jupyter_fallback_success_rc0()
    test_main_path_failure_rc1()
    test_infra_fail_rc1()
    test_git_diff_tool_returns_body_not_error_on_main_success()
    print("\n✅ P0-1 沙箱 git rc 判据全部通过")
