"""26 号文 F 路：knowledge 召回质量四条。

元结论原话：**一处噪声污染四层**——Layer A 的垃圾 → located_files → 依赖图扩展 +63
→ affected_files=86 → 又作 Layer B priority_files 与 Layer D 种子。
"""
from __future__ import annotations

import inspect

import pytest

from swarm.knowledge.retriever import SwarmRetriever, _mark_partial
from swarm.knowledge.service import format_brain_knowledge_prompt, format_layer_items


# ══════════════════════════════════════════════
# F-C1：Layer A「精确定位」实为项目名子串匹配
# ══════════════════════════════════════════════

class _FakeStruct:
    """按名称/类名查恒空；文件名通道按真实 SQL 语义模拟（含判别力预检）。"""

    def __init__(self, files_by_kw, total=100):
        self.files_by_kw = files_by_kw
        self.total = total
        self.calls: list[str] = []

    async def query_symbols_by_name(self, pid, kw):
        return []

    async def query_symbols_by_class(self, pid, kw):
        return []

    async def query_symbols_by_file_keyword(self, pid, kw, limit=30):
        self.calls.append(kw)
        hits = self.files_by_kw.get(kw, [])
        if self.total and len(hits) / self.total > 0.30:
            return []          # 与 structure_index 的判别力预检同语义
        return [{"file_path": f, "symbol_name": f.rsplit("/", 1)[-1]} for f in hits[:limit]]


def _retriever(struct):
    r = SwarmRetriever.__new__(SwarmRetriever)
    r._struct = struct
    return r


@pytest.mark.asyncio
async def test_first_keyword_no_longer_eats_every_slot():
    """★实测 `kw='ruo'` 命中 3523 条＝全库，25 个槽位被第一个关键词吃光★
    返回的是 `RuoYiApplication`、6 条重复包声明、demo 域 `GoodsModel`——
    `SysLoginController` 一次都没出现。5745 条历史日志里 `struct=25` 恒定饱和。
    改为分桶轮转：每个关键词都能进货（与 validate_assertions 的 D8② 分桶配额同思路）。"""
    struct = _FakeStruct({
        "ruo": [f"ruoyi/x{i}.java" for i in range(30)],      # 30% 以内，不触发弃用
        "login": ["ruoyi-admin/SysLoginController.java"],
    })
    out = await _retriever(struct)._retrieve_layer_a("p1", ["ruo", "login"])
    paths = [r["file_path"] for r in out]
    assert any("SysLoginController" in p for p in paths), \
        f"第二个关键词的命中必须进得来（当前 {len(paths)} 条全来自第一个）"


@pytest.mark.asyncio
async def test_no_discriminating_keyword_is_dropped():
    """项目名类关键词命中全库 → 判别力为零，整条弃用（否则它只会把槽位吃光）。"""
    struct = _FakeStruct({"ruo": [f"ruoyi/x{i}.java" for i in range(100)]}, total=100)
    out = await _retriever(struct)._retrieve_layer_a("p1", ["ruo"])
    assert out == []


def test_file_keyword_gate_is_a_ratio_not_a_count():
    """★判据必须是【占比】而非绝对数★：绝对数阈值对大小仓一致性差——
    小仓 50 个文件全命中同样是无判别力，大仓 200 命中可能只占 2%。"""
    from swarm.knowledge.structure_index import _FILE_KEYWORD_MAX_HIT_RATIO
    assert 0 < _FILE_KEYWORD_MAX_HIT_RATIO < 1


def test_file_keyword_orders_by_specificity_not_alphabet():
    """★字母序让 `AlarmXxx` 恒排在 `SysLoginController` 前，与相关性无关★
    排序必须按匹配特异性：basename stem 全等 > basename 含 > 仅路径含。"""
    from swarm.knowledge import structure_index
    src = inspect.getsource(structure_index.StructureIndexer.query_symbols_by_file_keyword)
    assert "ORDER BY" in src and "CASE" in src
    assert src.index("CASE") < src.index("file_path, start_line"), "特异性排序必须在字母序之前"


# ══════════════════════════════════════════════
# F-C3：Qdrant 全宕时零机读信号
# ══════════════════════════════════════════════

def test_layer_b_no_longer_swallows_its_own_exception():
    """★层内自吞异常 ＝ 外层 try 永远收不到（26 号文 F-C3）★
    Qdrant 全宕时 semantic 返回 []，而 `semantic_error` 不产生、`retrieval_partial`
    不设置 → prompt 与"该项目无相关知识"**逐字不可分**。"""
    src = inspect.getsource(SwarmRetriever._retrieve_layer_b)
    i = src.index("BM25 降级检索失败")
    assert "raise" in src[i:i + 200], "必须重抛，让外层 stats['semantic_error'] 真正落账"
    assert "return []" not in src[i:i + 200]


def test_partial_marker_carries_layer_names_and_accumulates():
    """★消费者打的是"层 %s 不可用"——传 True 会打出"层 True 不可用"★
    多层同时降级要都看得见，后写不能覆盖先写。"""
    st: dict = {}
    st["retrieval_partial"] = _mark_partial(st, "semantic")
    st["retrieval_partial"] = _mark_partial(st, "norms")
    assert st["retrieval_partial"] == "semantic,norms"
    assert _mark_partial(st, "norms") == "semantic,norms", "幂等"


def test_partial_marker_has_a_consumer():
    """新账必须有人消费（复核盲区之一）——本键的消费者在 analyze 节点。"""
    from swarm.brain import nodes
    assert "retrieval_partial" in inspect.getsource(nodes.analyze)


# ══════════════════════════════════════════════
# F-H1：Layer C 死了 12 天没人知道
# ══════════════════════════════════════════════

def test_empty_layer_is_machine_readable():
    """★"空返回是正常返回而非异常" ＝ 这一层可以死 12 天没人知道（26 号文 F-H1）★
    实测 Layer C 对生产项目自 07-18 起恒为 0，跨 5+ 轮 live 全程零信号。
    零命中本身可能正常（项目确实没沉淀规范），但它必须是可被发现的事实。"""
    src = inspect.getsource(SwarmRetriever.retrieve_for_brain)
    assert 'stats["norms_empty"] = True' in src
    from swarm.brain import nodes
    assert "norms_empty" in inspect.getsource(nodes.analyze), "机读信号必须有人消费"


# ══════════════════════════════════════════════
# F-H3：召回进 prompt 时 provenance 被主动丢弃
# ══════════════════════════════════════════════

def test_semantic_items_carry_line_numbers():
    """★payload 里有 start_line/end_line 却只取 file_path（26 号文 F-H3）★
    于是大模型拿到"某文件里的一段话"，无从核对也无从引用——而相邻块把项目结构标为
    "事实依据，ground truth"。这与"需求条目必须带原文引文防幻觉"的既定标准明显不对称。"""
    out = format_layer_items(
        "semantic", [{"file_path": "src/A.java", "start_line": 10,
                      "end_line": 42, "content": "x"}], 5)
    assert out[0]["title"] == "src/A.java:10-42"


def test_semantic_without_line_numbers_still_renders():
    """没有行号的条目照常渲染（向后兼容，不因缺字段丢内容）。"""
    out = format_layer_items("semantic", [{"file_path": "src/A.java", "content": "x"}], 5)
    assert out[0]["title"] == "src/A.java"


@pytest.mark.parametrize("layer,item,key", [
    ("mistakes", {"error_type": "NPE", "last_seen_at": "2026-04-01T00:00:00"}, "NPE"),
    ("successes", {"pattern_name": "P", "created_at": "2026-01-05"}, "P"),
])
def test_memory_items_carry_recency(layer, item, key):
    """★三个月前的错题与昨天的错题等价对待，会让模型把早已修好的历史坑当现行约束★"""
    out = format_layer_items(layer, [item], 5)
    assert key in out[0]["title"] and "最近出现" in out[0]["title"]


def test_prompt_carries_staleness_disclaimer():
    """★召回是【线索】不是 ground truth★
    相邻块把项目结构标为"事实依据，ground truth"，而召回层全无免责——
    大模型会把一段可能陈旧的片段当权威。"""
    p = format_brain_knowledge_prompt(
        {"semantic": [{"file_path": "a.java", "start_line": 1, "content": "x"}]}, "q")
    assert "索引快照" in p and "以磁盘/diff 实况为准" in p
