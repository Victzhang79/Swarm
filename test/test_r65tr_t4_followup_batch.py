"""R65TR-T4 跟进批①：finish_reason 遥测回归定案与治本。

round64 T6 加的流收尾遥测在治后回放全程打 finish_reason=?——根因：只看
last_chunk，而 include_usage 开启时流末尾是 usage-only 空 choices chunk
（无 finish_reason），真值在倒数第二个 chunk 里被覆盖。治=流中即时捕获
（见非空即记），收尾兜底再看 last_chunk。
"""

from __future__ import annotations

import asyncio
import logging

import pytest


class _Msg:
    def __init__(self, content, response_metadata=None):
        self.content = content
        self.additional_kwargs = {}
        self.response_metadata = response_metadata or {}


class _Chunk:
    def __init__(self, content, generation_info=None, response_metadata=None):
        self.content = content
        self.text = content
        self.generation_info = generation_info
        self.message = _Msg(content, response_metadata)


def _mk_llm():
    from swarm.models.router import _DualTimeoutChatOpenAI
    return _DualTimeoutChatOpenAI(
        model="probe", api_key="k", base_url="http://127.0.0.1:9",
    )


@pytest.mark.parametrize("tail_usage_chunk", [True, False])
def test_finish_reason_captured_despite_usage_tail(monkeypatch, caplog, tail_usage_chunk):
    """真 finish_reason 在倒数第二 chunk（后跟 usage 空尾）也必须被捕获。"""
    import langchain_openai

    llm = _mk_llm()
    object.__setattr__(llm, "swarm_heartbeat_after", 0.0)
    object.__setattr__(llm, "swarm_degen_enabled", False)

    chunks = [
        _Chunk("hello "),
        _Chunk("world", generation_info={"finish_reason": "length"}),
    ]
    if tail_usage_chunk:
        chunks.append(_Chunk("", generation_info={}))  # usage-only 尾 chunk

    async def _fake_astream(self, *a, **k):
        for c in chunks:
            yield c

    monkeypatch.setattr(langchain_openai.ChatOpenAI, "_astream", _fake_astream)

    async def _drain():
        out = []
        async for c in llm._astream_inner():
            out.append(c)
        return out

    with caplog.at_level(logging.INFO):
        got = asyncio.run(_drain())
    assert len(got) == len(chunks)
    lines = [r.getMessage() for r in caplog.records if "流式完成" in r.getMessage()]
    assert lines, "心跳阈值 0 应打收尾行"
    assert "finish_reason=length" in lines[-1], \
        f"usage 尾 chunk 不得覆盖真 finish_reason: {lines[-1]}"


def test_finish_reason_unknown_stays_honest(monkeypatch, caplog):
    """全程无 finish_reason（供应端不吐）→ 诚实 '?' 不造假。"""
    import langchain_openai

    llm = _mk_llm()
    object.__setattr__(llm, "swarm_heartbeat_after", 0.0)
    object.__setattr__(llm, "swarm_degen_enabled", False)

    async def _fake_astream(self, *a, **k):
        yield _Chunk("only content")

    monkeypatch.setattr(langchain_openai.ChatOpenAI, "_astream", _fake_astream)

    async def _drain():
        async for _ in llm._astream_inner():
            pass

    with caplog.at_level(logging.INFO):
        asyncio.run(_drain())
    lines = [r.getMessage() for r in caplog.records if "流式完成" in r.getMessage()]
    assert lines and "finish_reason=?" in lines[-1], lines


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
