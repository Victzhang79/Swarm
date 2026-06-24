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


import contextlib as _contextlib
import logging as _logging


@_contextlib.contextmanager
def _capture(logger_name: str, level: int = _logging.INFO):
    """直接挂 handler 到具名 logger 抓日志——不依赖 caplog 的 root 传播，规避其它测试残留的
    logging.disable / propagate=False 造成的漏抓（全套件乱序时 caplog 会失灵）。"""
    _logging.disable(_logging.NOTSET)
    lg = _logging.getLogger(logger_name)
    msgs: list[str] = []

    class _H(_logging.Handler):
        def emit(self, r):
            msgs.append(r.getMessage())
    h = _H()
    old = lg.level
    lg.addHandler(h)
    lg.setLevel(level)
    try:
        yield msgs
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)


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


def test_heartbeat_silent_for_short_calls(monkeypatch, caplog):
    """治本（可观测）：短调用零心跳噪声——总时长未达 heartbeat_after 时一行不打。"""
    import logging

    import swarm.models.router as router_mod

    async def quick(self, *a, **k):
        for c in ("a", "b", "c"):
            yield c
    monkeypatch.setattr(ChatOpenAI, "_astream", quick, raising=False)
    # 心跳时钟恒定 → 远未达 after=60（patch 间接层，不碰 asyncio 自身的 time.monotonic）
    monkeypatch.setattr(router_mod, "_monotonic", lambda: 1.0)
    m = _mk(5, 5)
    got: list = []

    async def run():
        async for c in m._astream([]):
            got.append(c)
    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert got == ["a", "b", "c"]
    assert not any("流式生成中" in r.getMessage() for r in caplog.records)


def test_heartbeat_fires_for_long_calls(monkeypatch):
    """治本（可观测）：长流式调用每 heartbeat_every 秒打一行 elapsed，证明仍在吐 token、未挂死。"""
    import swarm.models.router as router_mod

    async def slow(self, *a, **k):
        for c in ("a", "b", "c", "d"):
            yield c
    monkeypatch.setattr(ChatOpenAI, "_astream", slow, raising=False)
    # 受控心跳时钟：t0=0；4 个 chunk 后依次 now=3,12,14,18；末尾 20=收尾完成日志读 elapsed
    seq = iter([0.0, 3.0, 12.0, 14.0, 18.0, 20.0])
    monkeypatch.setattr(router_mod, "_monotonic", lambda: next(seq))
    m = _mk(5, 5)
    m.swarm_heartbeat_after = 10.0
    m.swarm_heartbeat_every = 5.0
    got: list = []

    async def run():
        async for c in m._astream([]):
            got.append(c)
    with _capture("swarm.models.router") as msgs:
        asyncio.run(run())
    assert got == ["a", "b", "c", "d"]
    beats = [mm for mm in msgs if "流式生成中" in mm]
    assert len(beats) == 2  # now=12（首达 after 且距 t0≥every）与 now=18（距上次≥every）各一次


def test_wallclock_budget_fires(monkeypatch):
    """治本第三条腿：稳定吐但吐不完的 runaway——累计超 wallclock_budget 即抛 transient（早 fail-fast）。"""
    import swarm.models.router as router_mod

    async def stream(self, *a, **k):
        for c in ("a", "b", "c"):
            yield c
    monkeypatch.setattr(ChatOpenAI, "_astream", stream, raising=False)
    # t0=0；chunk1 now=3(<10 不触发)；chunk2 now=12(≥10 触发)
    seq = iter([0.0, 3.0, 12.0])
    monkeypatch.setattr(router_mod, "_monotonic", lambda: next(seq))
    m = _mk(50, 50)  # stall 超时给宽，确保只验 wall-clock 这条腿
    m.swarm_wallclock_budget = 10.0
    got: list = []

    async def run():
        async for c in m._astream([]):
            got.append(c)
    try:
        asyncio.run(run())
        assert False, "应触发总时长看门狗"
    except TransientInfraError as e:
        assert "超预算" in str(e)
        assert got == ["a"]  # 首 chunk 已吐，第二个 chunk 时判超预算中断
        assert classify_failure(e) == "transient"  # 同 stall：退避/fallback，绝不当 capability 换模型


def test_wallclock_budget_zero_disables(monkeypatch):
    """wallclock_budget=0 关闭看门狗——即便时长跳到很大也不中断（worker 热路径默认这样）。"""
    import swarm.models.router as router_mod

    async def stream(self, *a, **k):
        for c in ("a", "b", "c"):
            yield c
    monkeypatch.setattr(ChatOpenAI, "_astream", stream, raising=False)
    seq = iter([0.0, 100.0, 200.0, 300.0, 400.0])  # 时长狂跳但 budget=0 不该触发
    monkeypatch.setattr(router_mod, "_monotonic", lambda: next(seq))
    m = _mk(50, 50)
    m.swarm_wallclock_budget = 0.0
    got: list = []

    async def run():
        async for c in m._astream([]):
            got.append(c)
    asyncio.run(run())
    assert got == ["a", "b", "c"]  # 全部正常吐完，无中断


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"run {len(fns)} (use pytest for monkeypatch fixtures)")
