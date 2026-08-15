#!/usr/bin/env python3
"""P1-B：任务级墙钟 deadline 单测。

修前：worker LLM 不传 wallclock_budget（默认 0=关）、brain 无任务级 deadline → 失控任务
（replan 空转 / 卡节点）可无上限占沙箱/GPU。
修后：单次 Brain 执行段（run_task / 每次 resume）在图事件循环顶检查墙钟，超上限 →
raise TaskWallclockExceeded → run_task/resume 的 except 归一化 FAILED、finally 释放锁/沙箱/_task_running。

纯逻辑（helper + 异常契约 + 配置），不依赖 DB/graph。
"""

from __future__ import annotations

import asyncio
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
    """批25 GS-5w 换锁：原命题=源码序断言（_raise_if_wallclock_exceeded 调用在
    astream_events 之后的循环体内）。
    换成行为锁：真跑 _stream_brain_events——假图首事件前先 sleep 50ms，墙钟上限 1ms
    → 图事件循环顶闸门必须 raise TaskWallclockExceeded（P1-B：失控任务到点中止，
    防无上限占沙箱/GPU）。
    红条件：删掉/改坏循环顶墙钟闸门 → 流正常跑完不抛 → 本测试红。"""
    from swarm.brain import runner

    class _FakeGraph:
        async def astream_events(self, graph_input, config=None, version="v2"):
            await asyncio.sleep(0.05)  # 首事件到达时已远超 1ms 上限
            yield {"event": "on_chain_start", "name": "analyze", "data": {}}

    class _Cfg:
        task_deadline_s = 0.001          # 1ms：首事件循环顶必超
        task_deadline_per_subtask_s = 0.0

    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _FakeGraph())
    monkeypatch.setattr(runner.store, "get_task", lambda tid: {})
    monkeypatch.setattr(runner, "get_config", lambda: _Cfg())
    monkeypatch.setattr("swarm.tracing.brain_graph_config", lambda **kw: {})
    monkeypatch.setattr("swarm.models.ledger.attach", lambda *a, **k: None)  # 断 DB 写穿
    topic = runner.register_task_queue("t-wc-gate")
    try:
        with pytest.raises(TaskWallclockExceeded):
            asyncio.run(runner._stream_brain_events("t-wc-gate", {}, topic, project_id="p"))
    finally:
        runner._stop_watchdog("t-wc-gate")  # 看门狗属已关闭 loop，仅弹出登记防残留
        runner._task_queues.pop("t-wc-gate", None)  # 批25 R1 复核：队列登记也弹出（同批 p2de 两锁同形）


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


def test_resume_paths_handle_cancellederror(monkeypatch):
    """批25 GS-5w 换锁：原命题=源码断言（except asyncio.CancelledError 存在且后段含
    status="CANCELLED"）。
    换成行为锁：真调 resume_task / resume_planning，_stream_brain_events 注入
    CancelledError → update_task 必须落 CANCELLED（F3：取消是 BaseException，
    不被 except Exception 捕获；漏显式处理会把任务卡在 ANALYZING/IN_REVISION
    直到重启对账）。
    红条件：删掉任一函数的 CancelledError 分支 → CancelledError 照样抛出但
    update_task 无 CANCELLED 记录 → 本测试红。"""
    from swarm.brain import runner

    update_calls: list[dict] = []

    async def _boom(*a, **k):
        raise asyncio.CancelledError()

    class _FakeLock:
        def __init__(self, *a, **k):
            pass

        def acquire(self):
            return True

        def release(self):
            return None

        def renew(self):
            return True

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", _FakeLock)
    monkeypatch.setattr(runner, "_stream_brain_events", _boom)
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "project_id": "p"})
    monkeypatch.setattr(runner.store, "get_project", lambda pid: {"path": None})
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: update_calls.append(kw))
    monkeypatch.setattr("swarm.infra.degrade.record_degrade", lambda *a, **k: None)
    monkeypatch.setattr("swarm.models.ledger.attach", lambda *a, **k: None)
    monkeypatch.setattr("swarm.models.ledger.detach", lambda *a, **k: None)

    for fn, args in ((runner.resume_task, ("t-cancel-rt", "approved")),
                     (runner.resume_planning, ("t-cancel-rp", {"decision": "approve"}))):
        update_calls.clear()
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(fn(*args))
        assert any(kw.get("status") == "CANCELLED" for kw in update_calls), \
            f"{fn.__name__} 取消分支未落 CANCELLED（F3 回归）"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
