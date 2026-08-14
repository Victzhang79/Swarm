#!/usr/bin/env python3
"""30 号文批9 M-1+M-2+F-4 锁：检索 degraded 消费链 + 零命中信号派生化 + worker 侧消费面。

- M-1①：`retrieval_degraded`（语义召回整体退化为 BM25 关键词召回）在 analyze 有
  【独立】消费分支——不并入 retrieval_partial（partial=某层挂了缺一块；degraded=
  每块都在但相关度整体降档，Brain 态度不同），且不被 elif 链吞掉（共存时两条都报）。
- M-1②：`_embed_degraded_warned` 与 `_embed_degraded_active` 同源同周期（每次检索
  前一起清）——原实现 warned 进程级粘滞，首次降级 WARNING 一次之后，后续每次检索
  的降级全程零信号。形态=「每次检索至多一次」（否决 always-emit 淹日志）。
- M-2：各层零命中统一写 `<layer>_empty`（与各自 `<layer>_count` 同源同点）；analyze
  消费枚举改派生 `endswith("_empty")`——新层只在源头写自己的键，消费端零维护
  （原手抄 ("norms_empty",) 单键枚举，其余 5 层零命中照旧静默）。
- F-4：worker 侧 query_knowledge_base 同样消费 retrieval_partial / retrieval_degraded
  ——小模型拿到残缺/弱相关上下文时必须被告知，否则把"没召回到"当"不存在"用。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarm.knowledge.retriever import SwarmRetriever
from swarm.knowledge.service import _empty_knowledge

# 权威检索层列表【派生】自 _empty_knowledge()（去掉非检索键）——绝不手抄枚举
# （手抄枚举必再漏 = M-2 原病本身；新层入库时本表自动跟随）。
_NON_RETRIEVAL_KEYS = {"project_summary", "preprocess_stats"}
_RETRIEVAL_LAYERS = sorted(k for k in _empty_knowledge() if k not in _NON_RETRIEVAL_KEYS)

_ITEM = {"file_path": "src/A.java", "content": "x"}


def _out(value):
    """层返回或层异常：值为 Exception 实例时模拟该层抛错（走 <layer>_error 路径）。"""
    if isinstance(value, Exception):
        raise value
    return list(value)


def _stubbed_retriever(layer_results: dict) -> SwarmRetriever:
    """全层 stub 的 retriever：每层返回 layer_results 配置的结果，其余通路全空。"""
    r = SwarmRetriever.__new__(SwarmRetriever)

    async def _meta(pid):
        return {}

    async def _layer_a(pid, kw):
        return _out(layer_results["struct"])

    async def _deps(pid, files, max_depth=2):
        return []

    async def _layer_b(pid, desc, files=None, keywords=None, degrade=None):
        return _out(layer_results["semantic"])

    async def _layer_c(pid, desc, kw):
        return _out(layer_results["norms"])

    async def _layer_d(pid, files):
        return _out(layer_results["behavior"])

    async def _rerank_memory(desc, items):
        return items

    def _mem_mock(v):
        if isinstance(v, Exception):
            return AsyncMock(side_effect=v)
        return AsyncMock(return_value=list(v))

    r._load_project_meta = _meta
    r._retrieve_layer_a = _layer_a
    r._expand_dependency_files = _deps
    r._retrieve_layer_b = _layer_b
    r._retrieve_layer_c = _layer_c
    r._retrieve_layer_d = _layer_d
    r._rerank_memory = _rerank_memory
    r._rerank = lambda ctx, desc: ctx
    r._kb_config = SimpleNamespace(retrieval_top_k=5)
    r._memory = SimpleNamespace(
        query_mistakes=_mem_mock(layer_results["mistakes"]),
        query_successes=_mem_mock(layer_results["successes"]),
    )
    return r


# ─── M-2 生产侧：逐层零命中 → 只写自己的 `<layer>_empty` ───

@pytest.mark.asyncio
@pytest.mark.parametrize("empty_layer", _RETRIEVAL_LAYERS)
async def test_each_layer_empty_writes_only_its_own_empty_key(empty_layer):
    """M-2：逐层构造空返回——只有该层的 `<layer>_empty` 落账，别的层不冤报。
    判据：本测试红 = 某层的零命中没写自己的机读键（写丢了/写成别人的）。"""
    results = {layer: [_ITEM] for layer in _RETRIEVAL_LAYERS}
    results[empty_layer] = []
    r = _stubbed_retriever(results)
    out = await r.retrieve_for_brain("fix A", "p1")
    for layer in _RETRIEVAL_LAYERS:
        key = f"{layer}_empty"
        if layer == empty_layer:
            assert out.stats.get(key) is True, \
                f"{layer} 空返回却没写 {key}——该层可以静默死很久（F-H1 原病复发）"
        else:
            assert key not in out.stats, f"{layer} 非空却冤报 {key}"


@pytest.mark.asyncio
async def test_no_empty_layer_writes_no_empty_keys():
    """全层非空 → 零 `_empty` 键（防 always-emit 冤报方向——信号淹没等于没有信号）。"""
    r = _stubbed_retriever({layer: [_ITEM] for layer in _RETRIEVAL_LAYERS})
    out = await r.retrieve_for_brain("fix A", "p1")
    assert not [k for k in out.stats if k.endswith("_empty")], out.stats


# ─── analyze 消费侧 harness（mock LLM + mock retrieve_knowledge）───

async def _run_analyze(stats: dict) -> None:
    from swarm.brain.nodes import analyze

    mock_context = {layer: [] for layer in _RETRIEVAL_LAYERS}
    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        resp = MagicMock()
        resp.content = ('{"complexity": "simple", "reasoning": "t", "key_risks": [],'
                        ' "suggested_subtask_count": 1}')
        mock_llm.return_value.ainvoke = AsyncMock(return_value=resp)
        with patch(
            "swarm.knowledge.service.retrieve_knowledge",
            new=AsyncMock(return_value=(mock_context, stats)),
        ):
            await analyze({"task_description": "t", "project_id": "p"})


# ─── M-2 消费侧：零命中层枚举派生，逐层可辨 ───

@pytest.mark.asyncio
@pytest.mark.parametrize("layer", _RETRIEVAL_LAYERS)
async def test_analyze_empty_layer_enumeration_is_derived(layer, caplog):
    """M-2 消费端：stats 里任意 `<layer>_empty=True` 都必须被 WARNING 点名——
    枚举派生自键本身。判据：本测试红 = 消费端回到了手抄键清单（新层照旧静默）。"""
    with caplog.at_level(logging.WARNING):
        await _run_analyze({f"{layer}_empty": True})
    msgs = [r.getMessage() for r in caplog.records if "零命中" in r.getMessage()]
    assert msgs and layer in msgs[0], \
        f"{layer}_empty 置位而 analyze 零命中 WARNING 未点名该层: {msgs}"


# ─── M-1①：retrieval_degraded 独立消费分支 ───

@pytest.mark.asyncio
async def test_analyze_degraded_has_independent_branch(caplog):
    """M-1①：retrieval_degraded 必须有独立 WARNING（此前零消费者=死字段，
    Brain 拿关键词召回当完整语义召回规划）。"""
    with caplog.at_level(logging.WARNING):
        await _run_analyze({"retrieval_degraded": "embed_unavailable_bm25_only"})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("整体降级" in m and "embed_unavailable_bm25_only" in m for m in msgs), msgs


@pytest.mark.asyncio
async def test_analyze_partial_and_degraded_both_reported(caplog):
    """partial 与 degraded 可同时成立——degraded 若用 elif 串进失败链会被静默吞掉
    （"缺席必须机读可辨"硬检查）。"""
    stats = {"retrieval_partial": "norms",
             "retrieval_degraded": "embed_unavailable_bm25_only"}
    with caplog.at_level(logging.WARNING):
        await _run_analyze(stats)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("部分降级" in m for m in msgs), msgs
    assert any("整体降级" in m for m in msgs), "elif 串链会把 degraded 静默吞掉"


@pytest.mark.asyncio
async def test_analyze_degraded_distinct_from_partial_wording(caplog):
    """两分支文案必须可分：degraded=召回质量整体降档，partial=某层不可用——
    合并成一个分支会让 Brain 采取错态度（M-1① 否决合并的原因）。"""
    with caplog.at_level(logging.WARNING):
        await _run_analyze({"retrieval_degraded": "embed_unavailable_bm25_only"})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("没召回到" in m for m in msgs), \
        "degraded 分支必须告知 Brain 正确态度：勿把'没召回到'当'不存在'"


# ─── M-1②/R1：降级状态=调用级局部态（hunter M 3.2 折入后重设计）───

def _bm25_retriever(embed_ok: bool) -> SwarmRetriever:
    """真实 _retrieve_layer_b + mock semantic 的 retriever（其余层全 stub）。
    embed_ok=False 时 embed 抛错 → 走 BM25 降级分支。"""
    import types

    from swarm.knowledge.semantic_index import BGE_M3_DIMENSION

    r = _stubbed_retriever({layer: [_ITEM] for layer in _RETRIEVAL_LAYERS})
    sem = MagicMock()
    if embed_ok:
        healthy = [[0.0] * BGE_M3_DIMENSION]
        healthy[0][0] = 0.8
        sem._embed_fn = AsyncMock(return_value=healthy)
    else:
        sem._embed_fn = AsyncMock(side_effect=RuntimeError("embed down"))
    sem.search = AsyncMock(return_value=[])
    sem.search_with_rerank = AsyncMock(
        return_value=[{"id": "VEC", "content": "v", "score": 0.9}])
    sem.bm25_only_search = AsyncMock(
        return_value=[{"id": "BM25", "content": "k", "score": 0.5}])
    r._semantic = sem
    # stub 替换掉整层后把真实 layer_b 绑回实例（本组测试要验的正是真实 BM25 分支）
    r._retrieve_layer_b = types.MethodType(SwarmRetriever._retrieve_layer_b, r)
    r._kb_config = SimpleNamespace(
        retrieval_top_k=5, rerank_top_k=5, max_priority_files=5, priority_file_top_k=3)
    return r


@pytest.mark.asyncio
async def test_bm25_degrade_warns_once_per_retrieval_and_counts(caplog):
    """M-1② 形态锁=每次检索至多一次 WARNING（BM25 分支每次检索只进一次，原进程级
    warned 粘滞门已随 R1 重设计退役）；且机读计数入 /api/metrics 降级账
    （hunter M 2.1：长期降级可聚合监控，不靠人工从日志噪声里捞 WARNING）。"""
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts

    reset_degrade_counts()
    r = _bm25_retriever(embed_ok=False)
    with caplog.at_level(logging.WARNING, logger="swarm.knowledge.retriever"):
        out = await r.retrieve_for_brain("q", "p1")
    warns = [x for x in caplog.records if "BM25" in x.getMessage()]
    assert len(warns) == 1, f"单次检索降级必须恰好一次 WARNING（实报 {len(warns)}）"
    assert out.stats.get("retrieval_degraded") == "embed_unavailable_bm25_only", \
        "降级必须落 stats（B11 透传不破，R1 起由 retrieve_for_brain 直写）"
    assert degrade_counts().get("knowledge.layer_b.embed_bm25_fallback", 0) >= 1, \
        "降级必须有机读计数（长期降级监控聚合面）"
    reset_degrade_counts()


@pytest.mark.asyncio
async def test_concurrent_retrievals_degrade_signals_do_not_interfere():
    """hunter M 3.2 主锁：并发检索共用同一 retriever 单例（生产形态——所有检索都
    跑在同一 KB loop 的单例上）——A 降级、B 健康并行，A 的 retrieval_degraded
    绝不得被 B 抹掉/串扰。
    判据：降级状态若改回实例属性（B 开头重置会清掉 A 飞行中已置的标记）→ 本测试红。"""
    import asyncio

    from swarm.knowledge.semantic_index import BGE_M3_DIMENSION

    r = _bm25_retriever(embed_ok=False)
    gate = asyncio.Event()
    healthy = [[0.0] * BGE_M3_DIMENSION]
    healthy[0][0] = 0.8

    async def _embed(texts):
        if texts and texts[0] == "A":
            raise RuntimeError("embed down")
        return healthy

    async def _bm25(pid, query_terms=None, **kw):
        gate.set()                  # A 已进入降级深处（degrade["active"] 已置）
        await asyncio.sleep(0.05)   # 让出 loop，给 B 完整跑完的窗口
        return [{"id": "BM25", "content": "k"}]

    r._semantic._embed_fn = _embed
    r._semantic.bm25_only_search = _bm25

    a = asyncio.create_task(r.retrieve_for_brain("A", "p1"))
    await gate.wait()                               # 等 A 挂上降级标记并停在 BM25 IO
    out_b = await r.retrieve_for_brain("B", "p1")   # B 完整跑完
    out_a = await a
    assert out_a.stats.get("retrieval_degraded") == "embed_unavailable_bm25_only", \
        "A 的降级信号被并发 B 抹掉=单例态竞争（hunter M 3.2 原病复发）"
    assert "retrieval_degraded" not in out_b.stats, "A 的降级串扰到健康的 B"


# ─── F-4：worker 侧消费面 ───

def _invoke_worker_tool(stats: dict) -> str:
    from swarm.knowledge.service import set_worker_context
    from swarm.tools.knowledge_tools import query_knowledge_base

    ctx = {layer: [] for layer in _RETRIEVAL_LAYERS}
    try:
        with patch(
            "swarm.tools.knowledge_tools.retrieve_knowledge_sync",
            return_value=(ctx, stats),
        ):
            set_worker_context("proj-1")
            return query_knowledge_base.invoke({"query": "q", "top_k": 3})
    finally:
        set_worker_context(None)


def test_worker_tool_consumes_retrieval_partial():
    """F-4：worker 侧必须被告知哪些层不可用——否则小模型把残缺上下文当完整事实。"""
    out = _invoke_worker_tool({"retrieval_partial": "norms"})
    assert "⚠️" in out and "部分降级" in out and "norms" in out, out


def test_worker_tool_consumes_retrieval_degraded():
    """F-4：语义召回降级为关键词召回必须写进返回文本（M-1 的第二消费面）。"""
    out = _invoke_worker_tool({"retrieval_degraded": "embed_unavailable_bm25_only"})
    assert "降级为关键词召回" in out and "embed_unavailable_bm25_only" in out, out


def test_worker_tool_no_notice_when_healthy():
    """健康检索零 ⚠️ 前缀（防 always-emit：每条输出都带警告=警告被无视）。"""
    ctx_item = {"struct": [{"symbol_name": "parse", "file_path": "parser.py",
                            "signature": "def parse"}],
                **{layer: [] for layer in _RETRIEVAL_LAYERS if layer != "struct"}}
    from swarm.knowledge.service import set_worker_context
    from swarm.tools.knowledge_tools import query_knowledge_base

    try:
        with patch(
            "swarm.tools.knowledge_tools.retrieve_knowledge_sync",
            return_value=(ctx_item, {"struct_count": 1}),
        ):
            set_worker_context("proj-1")
            out = query_knowledge_base.invoke({"query": "q", "top_k": 3})
    finally:
        set_worker_context(None)
    assert "⚠️" not in out, out
