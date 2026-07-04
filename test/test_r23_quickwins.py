"""round23 审计治本 — 快速项回归。

R23-8 打包入口：swarm-web 原指 ASGI 对象非可调用 → 新增 run() 入口 + pyproject 指向它。
R23-2 tsc 执行异常假绿：区分 infra(跳过) vs 非 infra(fail-closed 判不过)。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.worker import l1_pipeline


# ── R23-8 ──
def test_swarm_web_entrypoint_is_callable():
    from swarm.api.app import run
    assert callable(run)


def test_pyproject_swarm_web_points_to_run():
    import pathlib
    txt = pathlib.Path(__file__).resolve().parent.parent.joinpath("pyproject.toml").read_text()
    assert 'swarm-web = "swarm.api.app:run"' in txt


# ── R23-2 ──
def _compile_ts():
    return l1_pipeline._compile_files("/tmp/swarm-r23", ["src/app.ts"])


def test_tsc_infra_exception_skips_gate():
    """tsc 工具缺失(infra 异常) → 跳过闸门判过（不误伤合法 TS 任务）。"""
    with patch.object(l1_pipeline, "_manifest_present", return_value=True), \
         patch.object(l1_pipeline, "_run_check_split",
                      side_effect=FileNotFoundError("[Errno 2] No such file or directory: 'npx'")):
        ok, _ = _compile_ts()
    assert ok is True


def test_tsc_noninfra_exception_fail_closed():
    """tsc 超时/意外崩溃(非 infra 异常) → fail-closed 判未通过（不再假绿）。"""
    with patch.object(l1_pipeline, "_manifest_present", return_value=True), \
         patch.object(l1_pipeline, "_run_check_split", side_effect=RuntimeError("tsc crashed unexpectedly")):
        ok, detail = _compile_ts()
    assert ok is False, f"非 infra 的 tsc 异常必须判 False，got {ok} {detail!r}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
