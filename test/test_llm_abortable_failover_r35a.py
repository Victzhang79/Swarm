"""R35-A：_invoke_llm_abortable 外层墙钟超时→显式切备用模型(Kimi)。

取证（round35, E2E_ROUND35_REGISTER.md）：SiliconFlow 饱和时 GLM-5.2 稳定慢产（chunk
持续到达但整体 >300s），_invoke_llm_abortable 外层 wait_for 总超时在【消费者帧】抛
asyncio.TimeoutError，绕过 primary.with_fallbacks（那只兜 primary 于流【内】抛的异常）
→ 备用 Kimi 永不触发 → 同 GLM 空重试仍超时。治本：传 fallback_llm 时外层超时后主动切备。
"""

from __future__ import annotations

import asyncio

import pytest

from swarm.brain.nodes import _invoke_llm_abortable

_MSGS = [{"role": "user", "content": "hi"}]


class _StreamLLM:
    """流式桩：pre_delay 秒后逐块吐 chunks（delay 秒/块）；记录被调次数。"""

    def __init__(self, chunks, pre_delay=0.0, delay=0.0):
        self.chunks = chunks
        self.pre_delay = pre_delay
        self.delay = delay
        self.called = 0

    async def astream(self, messages):
        self.called += 1
        if self.pre_delay:
            await asyncio.sleep(self.pre_delay)
        for c in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield type("C", (), {"content": c})()


class _InvokeLLM:
    """非流式桩（无 astream）：走 wait_for(ainvoke) 分支。"""

    def __init__(self, content, delay=0.0):
        self.content = content
        self.delay = delay
        self.called = 0

    async def ainvoke(self, messages):
        self.called += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return type("R", (), {"content": self.content})()


# ─────────────── 流式路径 ───────────────

async def test_outer_timeout_switches_to_fallback(monkeypatch):
    """primary 首块前 hang 过总超时 → 外层 TimeoutError → 切备用，返回备用产物。"""
    monkeypatch.setenv("SWARM_PLAN_BATCH_CHUNK_GAP", "5")
    slow = _StreamLLM(["x"], pre_delay=5)  # 首块前 sleep 5s ≫ total 0.3
    fast = _StreamLLM(['{"ok":1}'])
    out = await _invoke_llm_abortable(slow, _MSGS, 0.3, fast)
    assert out.content == '{"ok":1}', "外层超时后必须返回备用模型产物"
    assert slow.called == 1 and fast.called == 1, "primary 试一次、备用切一次"


async def test_outer_timeout_no_fallback_raises(monkeypatch):
    """无备用模型 → 外层超时原样抛 asyncio.TimeoutError（调用方 timeout 分支处理）。"""
    monkeypatch.setenv("SWARM_PLAN_BATCH_CHUNK_GAP", "5")
    slow = _StreamLLM(["x"], pre_delay=5)
    with pytest.raises(asyncio.TimeoutError):
        await _invoke_llm_abortable(slow, _MSGS, 0.3)


async def test_primary_success_never_calls_fallback():
    """primary 正常吐流 → 绝不触碰备用模型（切备仅超时才发生）。"""
    fast = _StreamLLM(['{"a":1}'])
    fb = _StreamLLM(['{"b":2}'])
    out = await _invoke_llm_abortable(fast, _MSGS, 5, fb)
    assert out.content == '{"a":1}'
    assert fb.called == 0, "primary 成功时备用零调用"


async def test_fallback_also_times_out_propagates(monkeypatch):
    """备用也超时 → 抛 TimeoutError 给调用方（配合 R35-C 缓存回退兜底）。"""
    monkeypatch.setenv("SWARM_PLAN_BATCH_CHUNK_GAP", "5")
    slow1 = _StreamLLM(["x"], pre_delay=5)
    slow2 = _StreamLLM(["y"], pre_delay=5)
    with pytest.raises(asyncio.TimeoutError):
        await _invoke_llm_abortable(slow1, _MSGS, 0.3, slow2)
    assert slow2.called == 1, "备用被切过一次"


# ─────────────── 非流式（ainvoke 桩）路径向后兼容 ───────────────

async def test_ainvoke_stub_backward_compat():
    """无 astream 的桩 → 原 wait_for(ainvoke) 行为逐字节不变。"""
    llm = _InvokeLLM('{"x":1}')
    out = await _invoke_llm_abortable(llm, _MSGS, 5)
    assert out.content == '{"x":1}' and llm.called == 1


async def test_ainvoke_timeout_switches_to_fallback():
    """非流式 primary 超时也切备用（no-astream 分支同受护）。"""
    slow = _InvokeLLM("never", delay=5)
    fast = _InvokeLLM('{"ok":1}')
    out = await _invoke_llm_abortable(slow, _MSGS, 0.3, fast)
    assert out.content == '{"ok":1}' and fast.called == 1
