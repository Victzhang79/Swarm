#!/usr/bin/env python3
"""R63-T11：LLM 录制作用域——brain 图节点分派层统一打标签 + worker 流量双保险隔离。

round63 实锤（cassettes/round63/llm-41088.jsonl 10 行全 plan_batch）：录制钩门控
= env 开 AND _LLM_NODE_CV 非空，而全仓只有 4 个标签点（plan_batch/plan_single/
validate_plan/review:{tag}，都走 _invoke_llm_abortable）——tech_design/contract_design/
extract_requirements 等其余 brain 节点全部直连 llm.ainvoke 无标签 → 直通不录。
brain LLM streaming=True（router.py:914），ainvoke 也走 _astream，缺的只是标签。

治本＝graph 注册层 _maybe_labeled(name, fn)：默认给节点包 CV 标签（每节点一次），
denylist 排除 dispatch/monitor——contextvar 会经 asyncio.ensure_future（dispatch.py:610
spawn worker 任务）拷贝进 worker 上下文，包了它们=worker 流量被误录。双保险：worker
_run_agent 入口 set_llm_node("")，即使未来有节点泄漏标签，worker agent 流量也绝不带标签。
"""
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _cv():
    from swarm.models.router import _LLM_NODE_CV
    return _LLM_NODE_CV


# ── wrapper 机制 ──

@pytest.mark.asyncio
async def test_labeled_wrapper_async_sets_and_resets():
    from swarm.brain.graph import _maybe_labeled

    seen = {}

    async def _node(state):
        seen["label"] = _cv().get()
        return {"ok": True}

    wrapped = _maybe_labeled("tech_design", _node)
    assert wrapped is not _node, "非 denylist 节点必须被包装"
    out = await wrapped({"s": 1})
    assert out == {"ok": True}
    assert seen["label"] == "tech_design", "节点执行期间 CV 必须是节点名（录制门控据此放行）"
    assert _cv().get() == "", "执行后必须还原（不泄漏到后续调用）"


def test_labeled_wrapper_sync_supported():
    """_increment_plan_retry 是同步节点（graph.py:458）——wrapper 必须双支持。"""
    from swarm.brain.graph import _maybe_labeled

    seen = {}

    def _node(state):
        seen["label"] = _cv().get()
        return {"ok": True}

    wrapped = _maybe_labeled("increment_retry", _node)
    assert wrapped({"s": 1}) == {"ok": True}
    assert seen["label"] == "increment_retry"
    assert _cv().get() == ""


@pytest.mark.asyncio
async def test_labeled_wrapper_resets_on_exception():
    from swarm.brain.graph import _maybe_labeled

    async def _boom(state):
        raise RuntimeError("node failed")

    wrapped = _maybe_labeled("analyze", _boom)
    with pytest.raises(RuntimeError):
        await wrapped({})
    assert _cv().get() == "", "异常路径也必须还原标签"


@pytest.mark.asyncio
async def test_nested_finer_label_wins_and_restores():
    """既有细粒度标签（_invoke_llm_abortable 的 plan_batch 等）在节点标签内层设置
    时必须赢，并在 reset 后还原回节点标签——两层不许互相破坏。"""
    from swarm.brain.graph import _maybe_labeled
    from swarm.models.router import reset_llm_node, set_llm_node

    seen = {}

    async def _node(state):
        seen["outer"] = _cv().get()
        tok = set_llm_node("plan_batch")
        seen["inner"] = _cv().get()
        reset_llm_node(tok)
        seen["restored"] = _cv().get()
        return {}

    await _maybe_labeled("plan", _node)({})
    assert seen == {"outer": "plan", "inner": "plan_batch", "restored": "plan"}


def test_signature_transparent_for_langgraph():
    """LangGraph 按 inspect.signature 决定给节点传什么——包装必须签名透明。"""
    import inspect

    from swarm.brain.graph import _maybe_labeled

    async def _node(state):
        return {}

    wrapped = _maybe_labeled("assess", _node)
    assert list(inspect.signature(wrapped).parameters) == ["state"]


# ── denylist：spawn 节点绝不包装 ──

def test_spawn_nodes_never_labeled():
    """★核心不变量★ dispatch/monitor 会 asyncio.ensure_future spawn worker 任务
    （dispatch.py:610），contextvar 随 spawn 拷贝——包了它们=worker 流量被误录。"""
    from swarm.brain.graph import _LLM_NODE_LABEL_DENYLIST, _maybe_labeled

    assert "dispatch" in _LLM_NODE_LABEL_DENYLIST
    assert "monitor" in _LLM_NODE_LABEL_DENYLIST

    def _fn(state):
        return {}

    assert _maybe_labeled("dispatch", _fn) is _fn, "dispatch 必须原样返回不包装"
    assert _maybe_labeled("monitor", _fn) is _fn


# ── graph 注册接线 ──

def test_graph_registers_labeled_nodes():
    """★接线锁（猎手 F3：全量参数化，不留 17 个未断言节点）★ 注册表里除 denylist
    外的【每一个】节点都必须带标签 marker；dispatch/monitor 必须没有。"""
    from swarm.brain.graph import (
        _LLM_NODE_LABEL_DENYLIST,
        GRAPH_NODE_REGISTRY,
        build_brain_graph,
    )

    g = build_brain_graph()

    def _label_of(name: str):
        spec = g.nodes[name]
        run = getattr(spec, "runnable", spec)
        for attr in ("afunc", "func"):
            fn = getattr(run, attr, None)
            if fn is not None and hasattr(fn, "__swarm_llm_node_label__"):
                return fn.__swarm_llm_node_label__
        return getattr(run, "__swarm_llm_node_label__", None)

    names = {n for n, _ in GRAPH_NODE_REGISTRY}
    assert len(names) >= 26, f"注册表疑似被截: {sorted(names)}"
    for name in sorted(names):
        if name in _LLM_NODE_LABEL_DENYLIST:
            assert _label_of(name) is None, f"spawn 节点 {name} 绝不许带标签"
        else:
            assert _label_of(name) == name, f"节点 {name} 必须在注册层被打标签"


# ── 对抗复核整改锁（猎手 F1/F4） ──

def test_reset_llm_node_failure_is_observable(caplog):
    """★猎手 F1 锁★ reset 失败=标签可能粘滞在错误节点名（粘滞比缺失更毒：cassette/
    看门狗日志把后续调用全归错节点）——必须 WARNING 可观测，且观测面绝不抛。"""
    import logging

    from swarm.models.router import reset_llm_node

    with caplog.at_level(logging.WARNING, logger="swarm.models.router"):
        reset_llm_node(object())  # 非法 token → TypeError 路径
    assert any("粘滞" in r.message for r in caplog.records), \
        "reset 失败静默吞掉=归因数据无声腐坏"


@pytest.mark.asyncio
async def test_graph_interrupt_resets_label():
    """★猎手 F4 锁★ confirm 等节点经 langgraph interrupt() 抛 GraphInterrupt——
    finally 必须照样还原标签（Exception 子类，显式点名不留想当然）。"""
    from langgraph.errors import GraphInterrupt
    from swarm.brain.graph import _maybe_labeled

    async def _node(state):
        raise GraphInterrupt()

    with pytest.raises(GraphInterrupt):
        await _maybe_labeled("confirm", _node)({})
    assert _cv().get() == ""


def test_wrapped_node_llm_call_recorded_end_to_end(tmp_path, monkeypatch):
    """★猎手 F4 锁（round63 缺口本体的端到端）★ 包装节点内部的 LLM 流量必须落
    cassette 行且 node 字段=节点名——锁 wrapper→contextvar→录制门控整条链。"""
    import json

    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    from swarm.brain.graph import _maybe_labeled
    from swarm.models import cassette_record as cass
    from swarm.models.router import _DualTimeoutChatOpenAI

    # 复位进程级录制句柄（同 test_cassette_record_task10 的约定）
    with cass._fh_lock:
        if cass._fh is not None:
            try:
                cass._fh.close()
            except Exception:  # noqa: BLE001
                pass
        cass._fh = None
        cass._fh_key = None
    cass._fail_count = 0
    monkeypatch.setenv("SWARM_CASSETTE_RECORD_DIR", str(tmp_path))

    class _Msg:
        def __init__(self, content):
            self.content = content
            self.additional_kwargs = {}
            self.response_metadata = {}

    class _Chunk:
        def __init__(self, content):
            self.content = content
            self.message = _Msg(content)

    class _Agen:
        def __init__(self):
            self._i = 0
            self._chunks = [_Chunk("设计"), _Chunk("完成")]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._i >= len(self._chunks):
                raise StopAsyncIteration
            c = self._chunks[self._i]
            self._i += 1
            return c

        async def aclose(self):
            pass

    monkeypatch.setattr(ChatOpenAI, "_astream", lambda self, *a, **k: _Agen())
    llm = _DualTimeoutChatOpenAI(api_key="test-dummy-key", model="test-model")

    async def _tech_design_node(state):
        return [c async for c in llm._astream([HumanMessage(content="设计需求")])]

    got = asyncio.run(_maybe_labeled("tech_design", _tech_design_node)({}))
    assert [c.content for c in got] == ["设计", "完成"], "录制必须透传不改流"
    lines = []
    for p in sorted(tmp_path.glob("llm-*.jsonl")):
        lines += [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    assert len(lines) == 1 and lines[0]["node"] == "tech_design", \
        f"round63 缺口：tech_design 流量必须落 cassette 且归因正确: {lines}"


# ── worker 双保险隔离 ──

@pytest.mark.asyncio
async def test_run_agent_clears_llm_node_label():
    """★双保险锁★ 即使调用上下文带着 brain 节点标签（未来某节点泄漏），worker
    _run_agent 的 agent LLM 流量也必须无标签（cassette 铁律：worker 流量不录）。"""
    from swarm.models.router import set_llm_node
    from swarm.worker.executor_agent import _AgentLoopMixin

    seen = {}

    class _Agent:
        async def ainvoke(self, payload, config=None):
            seen["label_during_agent"] = _cv().get()
            return {"messages": []}

    class _FakeExec(_AgentLoopMixin):
        def __init__(self):
            self._agent = {"agent": _Agent()}
            self.start_time = time.monotonic()
            self.max_execution_time = 60
            self.max_iterations = 10
            self.subtask = SimpleNamespace(
                id="st-t11", difficulty=SimpleNamespace(value="medium"))
            self.project_id = "p"
            self.task_id = "t"
            self.phase = SimpleNamespace(value="coding")

        def _log(self, _m):
            pass

        def _record_tool_telemetry(self, messages, step):
            pass

    async def _in_labeled_ctx():
        set_llm_node("dispatch")  # 模拟标签泄漏进 worker 任务上下文
        await _FakeExec()._run_agent("hi", step="code")

    await asyncio.get_event_loop().create_task(_in_labeled_ctx())
    assert seen["label_during_agent"] == "", \
        "worker agent LLM 调用期间 CV 必须为空（否则 worker 流量会被录进 cassette）"
