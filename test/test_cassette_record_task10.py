"""Task#10：模型层 LLM 录制钩测试。

覆盖铁律：
  1) 默认关（未设 SWARM_CASSETTE_RECORD_DIR）→ recording_enabled() False，router 直通零录制。
  2) 开启 + 带 brain 节点标签 → 一次调用落一行 cassette（schema/messages/chunks/n_chunks 忠实）。
  3) 仅 brain：router 层无节点标签（worker 路径）→ 不产 cassette 文件。
  4) fail-open：单 chunk 序列化失败 / 落盘失败都不中断 LLM 流（全部 chunk 照常 yield）。
  5) 失败调用（流中途抛）也录一行且 error 落字段，异常照常上抛（不吞）。
  6) 消费者早停（GeneratorExit）→ 底层流 aclose 被触发 + 仍落一行（保 GPU abort 链）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from swarm.models import cassette_record as cass
from swarm.models.router import _DualTimeoutChatOpenAI, reset_llm_node, set_llm_node

# ---- fake 流式构件 -------------------------------------------------------

class _Msg:
    def __init__(self, content, additional_kwargs=None, response_metadata=None):
        self.content = content
        self.additional_kwargs = additional_kwargs or {}
        self.response_metadata = response_metadata or {}


class _Chunk:
    """近似 langchain ChatGenerationChunk：既有顶层 .content（_astream_inner 读），
    又有 .message（recorder 读 reasoning/收尾元数据）。"""
    def __init__(self, content, additional_kwargs=None, response_metadata=None):
        self.content = content
        self.message = _Msg(content, additional_kwargs, response_metadata)


async def _fake_stream(chunks, *, raise_at=None, closed_flag=None):
    try:
        for i, c in enumerate(chunks):
            if raise_at is not None and i == raise_at:
                from swarm.models.errors import TransientInfraError
                raise TransientInfraError("boom mid-stream")
            yield c
    finally:
        if closed_flag is not None:
            closed_flag["closed"] = True


def _read_lines(d: Path) -> list[dict]:
    out = []
    for p in sorted(d.glob("llm-*.jsonl")):
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


def _reset_recorder_state():
    # 关闭并清进程级缓存句柄，避免跨用例句柄指向旧 tmp 目录
    with cass._fh_lock:
        if cass._fh is not None:
            try:
                cass._fh.close()
            except Exception:
                pass
        cass._fh = None
        cass._fh_key = None
    cass._fail_count = 0


# ---- 1) 默认关 ----------------------------------------------------------

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SWARM_CASSETTE_RECORD_DIR", raising=False)
    assert cass.recording_enabled() is False
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", "   ")  # 纯空白视同未设
    assert cass.recording_enabled() is False
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", "/some/dir")
    assert cass.recording_enabled() is True


# ---- 2) 完整录制一次调用 ------------------------------------------------

def test_records_full_call(tmp_path, monkeypatch):
    _reset_recorder_state()
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", str(tmp_path))
    chunks = [_Chunk("Hel"), _Chunk("lo",
                     additional_kwargs={"reasoning_content": "think"},
                     response_metadata={"finish_reason": "stop"})]

    async def run():
        got = []
        async for ch in cass.tee_record(
                _fake_stream(chunks),
                node="plan_batch", model="GLM-5.2", provider="siliconflow",
                args=([SystemMessage(content="sys"), HumanMessage(content="hi")],),
                kwargs={"stop": ["</end>"]}):
            got.append(ch)
        return got

    got = asyncio.run(run())
    assert [c.content for c in got] == ["Hel", "lo"], "所有 chunk 必须照常透传"

    lines = _read_lines(tmp_path)
    assert len(lines) == 1, "一次调用落且仅落一行"
    rec = lines[0]
    assert rec["schema"] == "swarm-llm-cassette/v1"
    assert rec["node"] == "plan_batch"
    assert rec["model"] == "GLM-5.2"
    assert rec["provider"] == "siliconflow"
    assert rec["stop"] == ["</end>"]
    assert rec["error"] is None
    assert rec["n_chunks"] == 2
    assert len(rec["chunks"]) == 2
    assert rec["chunks"][0]["content"] == "Hel"
    assert rec["chunks"][1]["additional_kwargs"]["reasoning_content"] == "think"
    assert rec["chunks"][1]["response_metadata"]["finish_reason"] == "stop"
    assert rec["request_sha"], "请求指纹须非空（供未来 match-by-request 重放）"
    # messages 忠实：两条，system + human
    assert len(rec["messages"]) == 2
    joined = json.dumps(rec["messages"], ensure_ascii=False)
    assert "sys" in joined and "hi" in joined


# ---- 3) fail-open：单 chunk 序列化失败不中断流 --------------------------

def test_failopen_chunk_serialize_error(tmp_path, monkeypatch):
    _reset_recorder_state()
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", str(tmp_path))

    def _boom(_chunk):
        raise RuntimeError("serialize kaboom")
    monkeypatch.setattr(cass, "_chunk_to_dict", _boom)

    chunks = [_Chunk("a"), _Chunk("b"), _Chunk("c")]

    async def run():
        return [ch async for ch in cass.tee_record(
            _fake_stream(chunks), node="plan", model="m", provider="p",
            args=([HumanMessage(content="x")],), kwargs={})]

    got = asyncio.run(run())
    assert [c.content for c in got] == ["a", "b", "c"], "chunk 序列化失败绝不能吞掉 LLM 流"
    lines = _read_lines(tmp_path)
    assert len(lines) == 1
    # 计数照增（n_chunks 在 append 之前累加），但 chunk 体因序列化失败为空
    assert lines[0]["n_chunks"] == 3
    assert lines[0]["chunks"] == []


def test_failopen_flush_error_does_not_break_stream(tmp_path, monkeypatch):
    _reset_recorder_state()
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", str(tmp_path))
    monkeypatch.setattr(cass, "_get_fh",
                        lambda d: (_ for _ in ()).throw(OSError("disk full")))
    chunks = [_Chunk("a"), _Chunk("b")]

    async def run():
        return [ch async for ch in cass.tee_record(
            _fake_stream(chunks), node="plan", model="m", provider="p",
            args=([HumanMessage(content="x")],), kwargs={})]

    got = asyncio.run(run())  # 落盘炸也不得抛
    assert [c.content for c in got] == ["a", "b"]


# ---- 5) 失败调用也录，异常照常上抛 --------------------------------------

def test_error_call_recorded_and_reraised(tmp_path, monkeypatch):
    _reset_recorder_state()
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", str(tmp_path))
    from swarm.models.errors import TransientInfraError
    chunks = [_Chunk("a"), _Chunk("b"), _Chunk("c")]

    async def run():
        got = []
        async for ch in cass.tee_record(
                _fake_stream(chunks, raise_at=1),
                node="contract_design", model="m", provider="p",
                args=([HumanMessage(content="x")],), kwargs={}):
            got.append(ch)
        return got

    with pytest.raises(TransientInfraError):
        asyncio.run(run())

    lines = _read_lines(tmp_path)
    assert len(lines) == 1, "失败调用同样值得重放——必须落一行"
    assert lines[0]["n_chunks"] == 1, "抛前只吐了 1 个 chunk"
    assert "TransientInfraError" in (lines[0]["error"] or "")


# ---- 6) 消费者早停：底层流 aclose 触发 + 仍落一行 -----------------------

def test_early_break_closes_stream_and_records(tmp_path, monkeypatch):
    _reset_recorder_state()
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", str(tmp_path))
    closed = {"closed": False}
    chunks = [_Chunk("a"), _Chunk("b"), _Chunk("c")]

    async def run():
        agen = cass.tee_record(
            _fake_stream(chunks, closed_flag=closed),
            node="plan", model="m", provider="p",
            args=([HumanMessage(content="x")],), kwargs={})
        got = []
        async for ch in agen:
            got.append(ch)
            break               # 早停
        await agen.aclose()     # 触发 GeneratorExit 清理链
        return got

    got = asyncio.run(run())
    assert [c.content for c in got] == ["a"]
    assert closed["closed"] is True, "早停必须把 GeneratorExit 透传给底层流（保 aclose→GPU abort 链）"
    lines = _read_lines(tmp_path)
    assert len(lines) == 1 and lines[0]["n_chunks"] == 1


# ---- 3'/brain-only：router 层集成，无节点标签不录 -----------------------

def _fake_super_astream_factory(chunks):
    class _Agen:
        def __init__(self):
            self._i = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i >= len(chunks):
                raise StopAsyncIteration
            c = chunks[self._i]
            self._i += 1
            return c

        async def aclose(self):
            pass

    def _fake(self, *a, **k):
        return _Agen()
    return _fake


def test_router_records_only_when_brain_node_tagged(tmp_path, monkeypatch):
    _reset_recorder_state()
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", str(tmp_path))
    llm = _DualTimeoutChatOpenAI(api_key="test-dummy-key", model="test-model")
    fake = _fake_super_astream_factory([_Chunk("x"), _Chunk("y")])

    async def drive_with_node():
        tok = set_llm_node("plan_batch")
        try:
            return [c async for c in llm._astream([HumanMessage(content="hi")])]
        finally:
            reset_llm_node(tok)

    async def drive_no_node():
        # 无 set_llm_node → 模拟 worker 路径
        return [c async for c in llm._astream([HumanMessage(content="hi")])]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ChatOpenAI, "_astream", fake)
        got1 = asyncio.run(drive_with_node())
    assert [c.content for c in got1] == ["x", "y"]
    lines = _read_lines(tmp_path)
    assert len(lines) == 1 and lines[0]["node"] == "plan_batch", "brain 调用必须录"

    # worker 路径（无节点标签）：不得新增 cassette 行
    _reset_recorder_state()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ChatOpenAI, "_astream", fake)
        got2 = asyncio.run(drive_no_node())
    assert [c.content for c in got2] == ["x", "y"], "worker 流照常透传"
    lines2 = _read_lines(tmp_path)
    assert len(lines2) == 1, "无节点标签的 worker 调用绝不产 cassette（仅录 brain）"


def test_router_disabled_env_no_recording(tmp_path, monkeypatch):
    _reset_recorder_state()
    monkeypatch.delenv("SWARM_CASSETTE_RECORD_DIR", raising=False)
    llm = _DualTimeoutChatOpenAI(api_key="test-dummy-key", model="test-model")
    fake = _fake_super_astream_factory([_Chunk("x")])

    async def drive():
        tok = set_llm_node("plan_batch")
        try:
            return [c async for c in llm._astream([HumanMessage(content="hi")])]
        finally:
            reset_llm_node(tok)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ChatOpenAI, "_astream", fake)
        got = asyncio.run(drive())
    assert [c.content for c in got] == ["x"]
    assert not list(tmp_path.glob("llm-*.jsonl")), "env 未设时即便有节点标签也零录制"


# ---- F1 复核：真实集成点弃流，cassette 必须【确定性】落盘（不靠 GC）------------

def test_router_abandon_flushes_cassette_deterministically(tmp_path, monkeypatch):
    """复核 F1 的核心：消费者在真实 llm._astream(...) 上早停并 aclose（brain finally 的真实
    行为），outer→tee_record 的 aclose 转发必须【确定性】跑完 tee_record.finally(_flush)——
    弃流调用的记录不能丢。用 gc.disable() + 在 aclose 返回后【当场】读盘断言，排除任何 GC/
    事件循环销毁期终结器的功劳（旧单测在 asyncio.run 收尾靠 GC 才 close，给了假信心）。"""
    import gc

    _reset_recorder_state()
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", str(tmp_path))
    llm = _DualTimeoutChatOpenAI(api_key="test-dummy-key", model="test-model")
    fake = _fake_super_astream_factory([_Chunk("x"), _Chunk("y"), _Chunk("z")])

    async def drive():
        tok = set_llm_node("plan_batch")
        try:
            gen = llm._astream([HumanMessage(content="hi")])
            got = []
            async for ch in gen:
                got.append(ch)
                break                       # 弃流
            await gen.aclose()              # 消费者显式关闭
            # 关键：aclose 一返回就当场读盘——此刻尚未离开 drive()、无任何 loop 收尾/GC 介入
            return got, _read_lines(tmp_path)
        finally:
            reset_llm_node(tok)

    gc.disable()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ChatOpenAI, "_astream", fake)
            got, lines_at_close = asyncio.run(drive())
    finally:
        gc.enable()

    assert [c.content for c in got] == ["x"]
    assert len(lines_at_close) == 1, "弃流的 brain 调用必须在 aclose 时确定性落一行（不靠 GC）"
    assert lines_at_close[0]["n_chunks"] == 1, "弃流前只消费了 1 个 chunk"
    assert "GeneratorExit" in (lines_at_close[0]["error"] or ""), "弃流应记为 GeneratorExit"


def test_chunk_generation_info_finish_reason_recorded():
    """R64-T6：ChatGenerationChunk 的 finish_reason 落在 generation_info（不在
    response_metadata）——round64 全 58 行 finish_reason 皆 None、事后无法判截断。
    msg 分支必须带上非空 generation_info；空时不发键（不膨胀正常 chunk）。"""
    class _GenChunk(_Chunk):
        def __init__(self, content, generation_info=None, **kw):
            super().__init__(content, **kw)
            self.generation_info = generation_info

    mid = cass._chunk_to_dict(_GenChunk("正文", generation_info=None))
    assert "generation_info" not in mid, "空 generation_info 不发键"
    last = cass._chunk_to_dict(_GenChunk("", generation_info={"finish_reason": "stop"}))
    assert last.get("generation_info", {}).get("finish_reason") == "stop", \
        "终止原因必须入带（round64 缺口）"
