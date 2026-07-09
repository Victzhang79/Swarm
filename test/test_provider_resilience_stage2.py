"""阶段2（登记册 §三 B1/B2/B3/B5/B6/B9）：Provider 弹性层 — 行为测试。

  B1[用户拍板更正]：云端商业 provider 同端点换模型可信，不强求跨端点；主备不同模型
    校验既有（_get_brain_fallback_llm 备==主跳过切备）——无需改码，登记册标更正。
  B2：流中 stall/runaway 抛 TransientInfraError，与外层墙钟超时同等切备
    （原只对 asyncio.TimeoutError 切，饱和最常见形态只能同模型空转）。
  B3：进程级熔断（models/breaker.py）——连续 k 次超时/stall 进冷却直接走备，
    半开放一个探针；无备时不熔（唯一出路不能关）。
  B5：progress-aware 双限——活跃流（仍在出 chunk）超软限延长至硬顶，不硬杀
    （杀活跃流=已付 token 全废+重付 input）；无进展仍按软限杀。
  B6：provider 级进程并发闸（云端默认 6，本地默认不闸——时间成本口径）。
"""

from __future__ import annotations

import asyncio

import pytest

from swarm.brain.nodes import _invoke_llm_abortable
from swarm.models import breaker
from swarm.models.errors import TransientInfraError


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    breaker._reset_for_tests()
    monkeypatch.delenv("SWARM_BREAKER_THRESHOLD", raising=False)
    monkeypatch.delenv("SWARM_BREAKER_COOLDOWN_S", raising=False)
    monkeypatch.delenv("SWARM_STREAM_PROGRESS_HARD_MULT", raising=False)
    yield
    breaker._reset_for_tests()


class _R:
    def __init__(self, content="ok"):
        self.content = content


class _StallLLM:
    """无 astream：ainvoke 抛 TransientInfraError（模拟流中 stall 被看门狗杀）。"""
    model_name = "m-stall"

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise TransientInfraError("stream 解码中途 超时 30s (stream stall timeout)")


class _OkLLM:
    model_name = "m-ok"

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _R("saved")


# ─────────────────── B2：stall 切备 ───────────────────

async def test_stall_switches_to_fallback():
    fb = _OkLLM()
    r = await _invoke_llm_abortable(_StallLLM(), [], 5.0, fb)
    assert r.content == "saved" and fb.calls == 1, (
        "TransientInfraError（stall）必须与墙钟超时同等切备")


async def test_double_stall_raises():
    with pytest.raises(TransientInfraError):
        await _invoke_llm_abortable(_StallLLM(), [], 5.0, _StallLLM())


async def test_stall_without_fallback_propagates():
    with pytest.raises(TransientInfraError):
        await _invoke_llm_abortable(_StallLLM(), [], 5.0, None)


# ─────────────────── B3：熔断 ───────────────────

def test_breaker_opens_after_threshold_and_half_opens(monkeypatch):
    monkeypatch.setenv("SWARM_BREAKER_COOLDOWN_S", "0.05")
    for _ in range(3):
        assert breaker.allow("m1") is True
        breaker.record_failure("m1")
    assert breaker.allow("m1") is False, "达阈值必须熔断"
    import time
    time.sleep(0.06)
    assert breaker.allow("m1") is True, "冷却期满半开放一个探针"
    assert breaker.allow("m1") is False, "半开只放一个探针"
    breaker.record_success("m1")
    assert breaker.allow("m1") is True, "探针成功闭合复位"


def test_breaker_probe_failure_reopens(monkeypatch):
    monkeypatch.setenv("SWARM_BREAKER_COOLDOWN_S", "0.05")
    for _ in range(3):
        breaker.record_failure("m2")
    import time
    time.sleep(0.06)
    assert breaker.allow("m2") is True  # 探针
    breaker.record_failure("m2")        # 探针失败
    assert breaker.allow("m2") is False, "探针失败必须重新熔断"


async def test_broken_primary_skipped_directly_to_fallback():
    for _ in range(3):
        breaker.record_failure("m-stall")  # 预热熔断
    primary = _StallLLM()
    fb = _OkLLM()
    r = await _invoke_llm_abortable(primary, [], 5.0, fb)
    assert r.content == "saved"
    assert primary.calls == 0, "熔断开启必须跳过 primary（不再烧墙钟全款）"


async def test_breaker_not_applied_without_fallback():
    """无备时不熔——唯一出路不能关（fail-open 对称）。"""
    for _ in range(3):
        breaker.record_failure("m-ok")
    only = _OkLLM()
    r = await _invoke_llm_abortable(only, [], 5.0, None)
    assert r.content == "saved" and only.calls == 1


async def test_success_resets_breaker_counter():
    breaker.record_failure("m-ok")
    breaker.record_failure("m-ok")
    ok = _OkLLM()
    await _invoke_llm_abortable(ok, [], 5.0, None)
    breaker.record_failure("m-ok")  # 成功已清零，这只是第 1 次
    assert breaker.allow("m-ok") is True


# ─────────────────── B5：progress-aware ───────────────────

class _SlowProgressLLM:
    """astream 每 0.03s 出一个 chunk，共 12 个（总 0.36s）——软限 0.15s 会硬杀活跃流。"""
    model_name = "m-slow"

    def astream(self, messages):
        async def _gen():
            for i in range(12):
                await asyncio.sleep(0.03)
                yield _R(f"c{i} ")
        return _gen()


class _NoProgressLLM:
    model_name = "m-dead"

    def astream(self, messages):
        async def _gen():
            await asyncio.sleep(10)
            yield _R("late")
        return _gen()


async def test_active_stream_extends_past_soft_limit():
    r = await _invoke_llm_abortable(_SlowProgressLLM(), [], 0.15, None)
    assert "c11" in r.content, (
        "仍在出 chunk 的活跃流不得被软限硬杀（已付 token 全废=双倍浪费）")


async def test_no_progress_killed_at_soft_limit():
    t0 = asyncio.get_event_loop().time()
    with pytest.raises(TimeoutError):  # asyncio.TimeoutError（3.11+ 同 builtin）
        await _invoke_llm_abortable(_NoProgressLLM(), [], 0.15, None)
    assert asyncio.get_event_loop().time() - t0 < 2.0, "无进展必须按软限杀（不等硬顶）"


async def test_hard_cap_still_kills_runaway(monkeypatch):
    monkeypatch.setenv("SWARM_STREAM_PROGRESS_HARD_MULT", "1")  # 硬顶=软限（关延长）
    with pytest.raises(TimeoutError):
        await _invoke_llm_abortable(_SlowProgressLLM(), [], 0.15, None)


# ─────────────────── B6：provider 并发闸 ───────────────────

async def test_provider_slot_limits_concurrency():
    from swarm.models.router import _provider_slot
    peak = {"now": 0, "max": 0}

    async def _one():
        sem = _provider_slot("prov-x", 2)
        async with sem:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.02)
            peak["now"] -= 1

    await asyncio.gather(*[_one() for _ in range(6)])
    assert peak["max"] <= 2, f"provider 并发必须被闸在 2（实测 {peak['max']}）"


async def test_provider_slot_disabled_for_zero_limit():
    from swarm.models.router import _provider_slot
    assert _provider_slot("prov-x", 0) is None
    assert _provider_slot("", 5) is None


def test_resolve_provider_concurrency_defaults():
    from swarm.models.router import _resolve_provider_concurrency

    class _P:
        kind = "cloud"
        max_concurrency = None

    assert _resolve_provider_concurrency(_P()) == 6  # 云端默认
    _P.kind = "local"
    assert _resolve_provider_concurrency(_P()) == 0  # 本地不闸（时间成本口径）
    _P.max_concurrency = 3
    assert _resolve_provider_concurrency(_P()) == 3  # 显式优先
