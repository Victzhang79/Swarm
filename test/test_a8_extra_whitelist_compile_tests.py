"""A8（round22）：run_compile/run_tests 不含 extra_whitelist，与 run_command 不一致。

根因：harness 把构建命令前缀放进 extra_whitelist（set_extra_whitelist）。run_command 用
`cfg.command_whitelist + get_extra_whitelist()`，但 run_compile/run_tests 只查
cfg.command_whitelist → 合法 harness 命令被拒 → agent 自验失败、多烧 fix 轮（效率 bug）。

治本：两处（含 run_tests 的 auto 检测）白名单都并入 get_extra_whitelist()，与 run_command 对齐。

行为测试：mock _worker_config（窄白名单）+ _run，验证 extra_whitelist 内命令被放行。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from swarm.tools import build_tools


def _narrow_cfg():
    cfg = MagicMock()
    cfg.command_whitelist = ["python -m py_compile"]  # 故意不含 mvn
    return cfg


def _with_sandbox_ctx():
    """1f3ef04 起 LLM 命令工具须远程沙箱上下文；本测试只验白名单并集，补一个伪上下文过新闸。"""
    build_tools.set_sandbox_context(MagicMock(name="sandbox"), MagicMock(name="manager"))


def test_run_compile_honors_extra_whitelist():
    with patch.object(build_tools, "_worker_config", return_value=_narrow_cfg()), \
         patch.object(build_tools, "_run", return_value="BUILD OK") as mock_run:
        _with_sandbox_ctx()
        build_tools.set_extra_whitelist(["mvn"])
        try:
            out = build_tools.run_compile.func(language="java")
        finally:
            build_tools.clear_extra_whitelist()
            build_tools.clear_sandbox_context()
    assert "被拒绝" not in out, f"extra_whitelist 内命令不应被拒: {out}"
    mock_run.assert_called_once()


def test_run_compile_still_rejects_when_in_neither():
    with patch.object(build_tools, "_worker_config", return_value=_narrow_cfg()), \
         patch.object(build_tools, "_run", return_value="X") as mock_run:
        _with_sandbox_ctx()
        build_tools.clear_extra_whitelist()
        try:
            out = build_tools.run_compile.func(language="java")
        finally:
            build_tools.clear_sandbox_context()
    assert "被拒绝" in out, "既不在 cfg 也不在 extra → 仍应拒绝（不放水）"
    mock_run.assert_not_called()


def test_run_tests_honors_extra_whitelist():
    with patch.object(build_tools, "_worker_config", return_value=_narrow_cfg()), \
         patch.object(build_tools, "_run", return_value="TESTS OK") as mock_run:
        _with_sandbox_ctx()
        build_tools.set_extra_whitelist(["mvn"])
        try:
            out = build_tools.run_tests.func(language="java")
        finally:
            build_tools.clear_extra_whitelist()
            build_tools.clear_sandbox_context()
    assert "被拒绝" not in out, f"extra_whitelist 内命令不应被拒: {out}"
    mock_run.assert_called_once()


def test_run_tests_auto_detect_uses_extra_whitelist():
    """auto 检测也应看 extra_whitelist（否则 harness-only 的 mvn test 检测不到）。"""
    with patch.object(build_tools, "_worker_config", return_value=_narrow_cfg()), \
         patch.object(build_tools, "_run", return_value="TESTS OK") as mock_run:
        _with_sandbox_ctx()
        build_tools.set_extra_whitelist(["mvn test"])
        try:
            out = build_tools.run_tests.func(language="auto")
        finally:
            build_tools.clear_extra_whitelist()
            build_tools.clear_sandbox_context()
    assert "无法自动检测" not in out and "被拒绝" not in out, out
    mock_run.assert_called_once()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== A8 extra_whitelist 对齐: {len(fns)}/{len(fns)} passed ===")
