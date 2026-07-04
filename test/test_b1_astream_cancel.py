"""B1（round22, P0）：任务取消时 LLM 流式连接不关闭 → 推理端继续空烧 GPU。

根因：_DualTimeoutChatOpenAI._astream 仅在 asyncio.TimeoutError / wall-clock runaway 时
调 agen.aclose()；cancel_task → handle.cancel() 在 `await asyncio.wait_for(agen.__anext__())`
上抛 asyncio.CancelledError，无对应分支 → 底层 HTTP 流不关 → vLLM/Ollama 继续解码占 GPU。

治本：新增 except asyncio.CancelledError 分支，与 TimeoutError 同处理——先 agen.aclose()
关底层流让推理端 abort 解码释放 GPU，再上抛（不吞取消语义）。

行为测试：patch 父类 _astream 返回一个 __anext__ 抛 CancelledError 的 fake agen，
断言我们的 _astream 调用了它的 aclose 且 CancelledError 上抛。
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_openai import ChatOpenAI

from swarm.models.router import _DualTimeoutChatOpenAI


class _FakeAgen:
    def __init__(self, record):
        self._record = record

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise asyncio.CancelledError()

    async def aclose(self):
        self._record["closed"] = True


def test_astream_closes_underlying_stream_on_cancel():
    record = {"closed": False}

    def fake_super_astream(self, *a, **k):
        return _FakeAgen(record)

    llm = _DualTimeoutChatOpenAI(api_key="test-dummy-key", model="test-model")

    async def run():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ChatOpenAI, "_astream", fake_super_astream)
            outer = llm._astream(["hi"])
            with pytest.raises(asyncio.CancelledError):
                await outer.__anext__()

    asyncio.run(run())
    assert record["closed"] is True, "取消时必须关闭底层流（否则推理端继续解码占 GPU）"


class _TwoChunkAgen:
    """回归：正常两 chunk 后 StopAsyncIteration，_astream 应正常 yield 后干净收尾。"""

    def __init__(self, record):
        self._record = record
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self._i += 1
        if self._i <= 2:
            return f"chunk{self._i}"
        raise StopAsyncIteration

    async def aclose(self):
        self._record["closed"] = True


def test_astream_normal_flow_yields_chunks():
    record = {"closed": False}

    def fake_super_astream(self, *a, **k):
        return _TwoChunkAgen(record)

    llm = _DualTimeoutChatOpenAI(api_key="test-dummy-key", model="test-model")

    async def run():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ChatOpenAI, "_astream", fake_super_astream)
            got = [c async for c in llm._astream(["hi"])]
        return got

    got = asyncio.run(run())
    assert got == ["chunk1", "chunk2"], got


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== B1 取消关流: {len(fns)}/{len(fns)} passed ===")
