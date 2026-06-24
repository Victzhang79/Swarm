#!/usr/bin/env python3
"""治本 A：流式双超时拆分（首 token 宽 / 解码间隔紧）。

旧单值 stream_chunk_timeout 把"等首 token(prefill，本就慢)"和"解码中途 stall(本该快)"混为一谈：
调大才容得下慢 prefill，但同时纵容真 stall。拆开后 prefill 有空间、真 stall 仍秒级抓——
ultra E2E "全是调用超时→空 diff→假失败" 的根治。超时抛 TransientInfraError(→ classify_failure
归 transient，退避重试/fallback，绝不当 capability 换模型，对齐治本 C)。
"""
from __future__ import annotations

import asyncio

from langchain_openai import ChatOpenAI

from swarm.models.errors import TransientInfraError, classify_failure
from swarm.models.router import _DualTimeoutChatOpenAI


def _mk(ftt: float, itt: float) -> _DualTimeoutChatOpenAI:
    return _DualTimeoutChatOpenAI(
        model="m", base_url="http://x", api_key="EMPTY",
        swarm_first_token_timeout=ftt, swarm_inter_chunk_timeout=itt)


def test_fields_accepted_and_router_builds():
    m = _mk(180, 30)
    assert m.swarm_first_token_timeout == 180 and m.swarm_inter_chunk_timeout == 30
    from swarm.models.router import ModelRouter
    llm = ModelRouter().get_model_by_name("Qwopus3.6-27B-v2-NVFP4")
    assert isinstance(llm, _DualTimeoutChatOpenAI)
    assert llm.swarm_first_token_timeout == 180.0 and llm.swarm_inter_chunk_timeout == 30.0


def test_first_token_timeout_fires(monkeypatch):
    async def never(self, *a, **k):
        await asyncio.sleep(10)
        yield "x"
    monkeypatch.setattr(ChatOpenAI, "_astream", never, raising=False)
    m = _mk(0.05, 5)

    async def run():
        async for _ in m._astream([]):
            pass
    try:
        asyncio.run(run())
        assert False, "应超时"
    except TransientInfraError as e:
        assert "首 token" in str(e)
        assert classify_failure(e) == "transient"  # 绝不当 capability 换模型


def test_inter_chunk_timeout_fires_after_first(monkeypatch):
    async def fast_then_stall(self, *a, **k):
        yield "a"
        await asyncio.sleep(10)
        yield "b"
    monkeypatch.setattr(ChatOpenAI, "_astream", fast_then_stall, raising=False)
    m = _mk(5, 0.05)
    got: list = []

    async def run():
        async for c in m._astream([]):
            got.append(c)
    try:
        asyncio.run(run())
        assert False, "应在解码中途超时"
    except TransientInfraError as e:
        assert "解码中途" in str(e)
        assert got == ["a"]  # 首 chunk 已正常吐出，stall 才中断


def test_normal_stream_passes_through(monkeypatch):
    async def normal(self, *a, **k):
        for c in ("a", "b", "c"):
            yield c
    monkeypatch.setattr(ChatOpenAI, "_astream", normal, raising=False)
    m = _mk(5, 5)
    got: list = []

    async def run():
        async for c in m._astream([]):
            got.append(c)
    asyncio.run(run())
    assert got == ["a", "b", "c"]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"run {len(fns)} (use pytest for monkeypatch fixtures)")
