"""B6（round22, P1）：vision ingest 用同步 invoke 跑在线程池，取消无法中断。

根因：understand_file 用 llm.invoke（同步）+ ingest_node 用 asyncio.to_thread → cancel_task
取消 asyncio 任务后线程内 LLM 仍跑到结束占 GPU（与 B1 叠加，ingest 阶段取消不可靠）。

治本：新增 understand_file_async（await ainvoke，可中断）；ingest_node 改走异步版。
"""
from __future__ import annotations

import asyncio

import pytest

from swarm.brain import vision_ingest


def test_async_cancel_propagates():
    """LLM 调用中取消 → CancelledError 上抛（不被吞成 result.error）。"""
    async def _cancel_during_llm(*a, **k):
        raise asyncio.CancelledError()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(vision_ingest, "select_vision_model", lambda: "m")
        mp.setattr(vision_ingest, "_image_to_data_url", lambda p: "data:image/png;base64,xx")
        mp.setattr(vision_ingest, "_ainvoke_vision", _cancel_during_llm)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(vision_ingest.understand_file_async("a.png", "image"))


def test_async_success_path():
    async def _ok(*a, **k):
        return "理解的文本内容"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(vision_ingest, "select_vision_model", lambda: "m")
        mp.setattr(vision_ingest, "_image_to_data_url", lambda p: "data:image/png;base64,xx")
        mp.setattr(vision_ingest, "_ainvoke_vision", _ok)
        r = asyncio.run(vision_ingest.understand_file_async("a.png", "image"))
    assert r.ok, r.error
    assert "理解的文本" in r.understanding


def test_async_no_model():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(vision_ingest, "select_vision_model", lambda: None)
        r = asyncio.run(vision_ingest.understand_file_async("a.png", "image"))
    assert not r.ok and "多模态模型" in (r.error or "")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
