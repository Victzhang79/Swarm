#!/usr/bin/env python3
"""P1-B：任务级墙钟 deadline 单测。

修前：worker LLM 不传 wallclock_budget（默认 0=关）、brain 无任务级 deadline → 失控任务
（replan 空转 / 卡节点）可无上限占沙箱/GPU。
修后：单次 Brain 执行段（run_task / 每次 resume）在图事件循环顶检查墙钟，超上限 →
raise TaskWallclockExceeded → run_task/resume 的 except 归一化 FAILED、finally 释放锁/沙箱/_task_running。

纯逻辑（helper + 异常契约 + 配置），不依赖 DB/graph。
"""

from __future__ import annotations

import time

import pytest

from swarm.brain.runner import (
    TaskWallclockExceeded,
    _effective_deadline_s,
    _raise_if_wallclock_exceeded,
)


def test_within_deadline_does_not_raise():
    _raise_if_wallclock_exceeded(time.monotonic(), 10.0)  # 刚开始，远未超时


def test_exceeded_deadline_raises():
    start = time.monotonic() - 100.0  # 假装已跑 100s
    with pytest.raises(TaskWallclockExceeded) as ei:
        _raise_if_wallclock_exceeded(start, 10.0)
    assert ei.value.deadline_s == 10.0
    assert ei.value.elapsed_s >= 100.0


def test_zero_deadline_disables_check():
    start = time.monotonic() - 10_000.0
    _raise_if_wallclock_exceeded(start, 0.0)  # 0 = 关闭，永不 raise


def test_negative_deadline_disables_check():
    start = time.monotonic() - 10_000.0
    _raise_if_wallclock_exceeded(start, -1.0)  # <=0 关闭


def test_exception_is_generic_exception_subclass():
    # 契约：TaskWallclockExceeded 必须是 Exception 子类，方能被 run_task/resume 的
    # `except Exception` 捕获并归一化为 FAILED + finally 释放资源。
    assert issubclass(TaskWallclockExceeded, Exception)
    exc = TaskWallclockExceeded(14400.0, 15000.0)
    assert "墙钟超时" in str(exc)


def test_config_has_task_deadline_default():
    from swarm.config.settings import AppConfig

    cfg = AppConfig()
    assert cfg.task_deadline_s == 21600.0  # 默认基线 6h
    assert cfg.task_deadline_per_subtask_s == 1200.0  # 每子任务 +20min 弹性预算


def test_stream_loop_calls_wallclock_check_at_top(monkeypatch):
    """装配守卫：_stream_brain_events 源码里图事件循环体顶部确实调用了墙钟闸门。"""
    import inspect

    from swarm.brain import runner

    src = inspect.getsource(runner._stream_brain_events)
    assert "_raise_if_wallclock_exceeded" in src, "流事件循环未接入墙钟闸门（P1-B 回归）"
    # 调用出现在 astream_events 循环之后（循环体内）
    assert src.index("_raise_if_wallclock_exceeded") > src.index("astream_events")


# ── 弹性预算（★不误杀大型任务★）+ F3/F4 对抗复核治本 ──────────────


def test_elastic_deadline_scales_with_subtasks():
    """★核心：弹性预算随子任务数放宽，大型任务不被基线上限误杀。★"""
    base, per = 21600.0, 1200.0  # 6h + 20min/子任务
    assert _effective_deadline_s(base, per, None) == base       # 规划前只用 base
    assert _effective_deadline_s(base, per, 0) == base
    assert _effective_deadline_s(base, per, 1) == base + per
    # 45 子任务的大型任务 → 6h + 15h = 21h，远超合法实测 7-8h，绝不误杀
    assert _effective_deadline_s(base, per, 45) == base + per * 45
    assert _effective_deadline_s(base, per, 45) >= 8 * 3600 * 2  # ≥16h 富余


def test_elastic_deadline_disabled_when_base_zero():
    assert _effective_deadline_s(0.0, 1200.0, 100) == 0.0  # base=0 关闭，子任务再多也不启用


def test_large_task_not_killed_at_base_deadline():
    """回归：一个已跑 7h（合法大型任务实测量级）的 45 子任务任务，弹性上限 21h 内 → 不 raise。"""
    base, per = 21600.0, 1200.0
    eff = _effective_deadline_s(base, per, 45)
    start = time.monotonic() - 7 * 3600  # 已跑 7h
    _raise_if_wallclock_exceeded(start, eff)  # 不抛（7h < 21h 弹性上限）


def test_negative_deadline_config_rejected(monkeypatch):
    """F4：负 SWARM_TASK_DEADLINE_S 是误配 → 构造 AppConfig 即 fail（不静默关闭保护）。"""
    from pydantic import ValidationError

    from swarm.config.settings import AppConfig

    monkeypatch.setenv("SWARM_TASK_DEADLINE_S", "-1")
    with pytest.raises(ValidationError):
        AppConfig()


def test_resume_paths_handle_cancellederror():
    """F3：resume_task / resume_planning 必须显式处理 CancelledError → 落 CANCELLED，
    否则取消途中任务卡在 ANALYZING/IN_REVISION 直到重启对账。"""
    import inspect

    from swarm.brain import runner

    for fn in (runner.resume_task, runner.resume_planning):
        src = inspect.getsource(fn)
        assert "except asyncio.CancelledError" in src, f"{fn.__name__} 缺 CancelledError 处理（F3 回归）"
        # 取消分支落 CANCELLED
        _cancel_seg = src[src.index("except asyncio.CancelledError"):]
        assert 'status="CANCELLED"' in _cancel_seg, f"{fn.__name__} 取消分支未落 CANCELLED"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
