"""R65B-T3 真混合候选并集单测。

round65b 复盘实锤：hybrid「融合」只在稠密 top-K 候选内重打分——关键词精确命中
若没进稠密候选（中文查询被文案富集的模板系统性抢占 top-20）就永远救不回。
治本：search_with_rerank 里 BM25 关键词臂（bm25_only_search）独立供给候选，
与稠密候选按 id 并集后再融合重排。

锁面：
- 并集：关键词臂独有命中必须进入最终结果池（不再被稠密候选门槛挡死）；
- 并集候选向量维度记 0（只凭关键词维度竞争，不伪造稠密分）；
- 零分噪声（scroll 顺序返回无关键词命中）不入池；
- 关键词臂失败 → 降级稠密路径，绝不阻断检索；
- 旋钮 hybrid_union_scroll_limit=0 → 完全关闭并集（回退旧行为）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.knowledge.semantic_index import SemanticIndexer


def _mk_indexer(dense, keyword, *, union_cap=5000, bm25_w=0.3,
                keyword_raises=False, calls=None):
    """object.__new__ 构造：只装 search_with_rerank 用到的成员。"""
    idx = object.__new__(SemanticIndexer)

    class _Cfg:
        retrieval_top_k = 20
        rerank_top_k = 5
        hybrid_bm25_weight = bm25_w
        hybrid_union_scroll_limit = union_cap

    idx._kb_config = _Cfg()

    async def _search(project_id, query, top_k=None, filter_dict=None,
                      query_vector=None):
        return [dict(c) for c in dense]

    async def _bm25_only(project_id, query_terms=None, top_k=None,
                         scroll_limit=None, per_file_cap=None):
        if calls is not None:
            calls.append({"query_terms": query_terms, "top_k": top_k,
                          "scroll_limit": scroll_limit,
                          "per_file_cap": per_file_cap})
        if keyword_raises:
            raise RuntimeError("qdrant scroll down")
        return [dict(c) for c in keyword]

    idx.search = _search
    idx.bm25_only_search = _bm25_only
    return idx


def _identity_rerank(query, candidates, top_k=5, text_key="content"):
    return candidates[:top_k]


def _run_swr(idx, query_terms):
    with patch("swarm.knowledge.reranker.rerank_documents", _identity_rerank):
        return asyncio.run(idx.search_with_rerank(
            "p1", "告警任务如何调度", query_vector=[0.1, 0.2],
            query_terms=query_terms))


_DENSE = [
    {"id": "d1", "score": 0.9, "content": "模板页面 告警 列表 表格 渲染"},
    {"id": "d2", "score": 0.8, "content": "通用 工具类 字符串 处理"},
]
# 关键词臂独有命中：包含查询词 selectAlarmTask（稠密没召回它）
_KW_HIT = {"id": "k1", "score": 3.2, "bm25_score": 3.2,
           "content": "public List<AlarmTask> selectAlarmTask(String jobId)"}
_KW_NOISE = {"id": "k2", "score": 0.0, "bm25_score": 0.0,
             "content": "无关 README 文档"}


def test_union_recovers_keyword_only_hit():
    """关键词臂独有命中必须进入最终结果（round65b 死面：稠密没召回=永远出局）。"""
    idx = _mk_indexer(_DENSE, [_KW_HIT, _KW_NOISE])
    out = _run_swr(idx, ["selectalarmtask", "告警"])
    assert any(c.get("id") == "k1" for c in out), \
        f"关键词臂独有命中被稠密候选门槛挡死: {[c.get('id') for c in out]}"


def test_union_candidate_vec_score_zeroed():
    """并集候选向量维度记 0——bm25_only_search 把 bm25 分写进 score 字段，
    直接并入会被融合当成稠密分（伪造语义相似度）。"""
    captured = {}

    def _capture_fuse(cands, terms, bm25_weight=0.3, text_key="content"):
        captured["cands"] = [dict(c) for c in cands]
        return cands

    idx = _mk_indexer(_DENSE, [_KW_HIT])
    with patch("swarm.knowledge.hybrid.hybrid_fuse", _capture_fuse), \
         patch("swarm.knowledge.reranker.rerank_documents", _identity_rerank):
        asyncio.run(idx.search_with_rerank(
            "p1", "q", query_vector=[0.1], query_terms=["selectalarmtask"]))
    k1 = next(c for c in captured["cands"] if c["id"] == "k1")
    assert k1["score"] == 0.0, f"并集候选稠密分必须清零: {k1}"


def test_union_filters_zero_bm25_noise():
    """scroll 顺序返回的零分噪声不入池（无关键词命中=无入场理由）。"""
    idx = _mk_indexer(_DENSE, [_KW_NOISE])
    out = _run_swr(idx, ["selectalarmtask"])
    assert not any(c.get("id") == "k2" for c in out), "零分噪声不应入池"


def test_union_dedupes_by_id():
    """同 id 候选（稠密已召回）不重复入池，保稠密原分。"""
    dup = {"id": "d1", "score": 2.0, "bm25_score": 2.0, "content": "模板页面 告警"}
    idx = _mk_indexer(_DENSE, [dup])
    out = _run_swr(idx, ["告警"])
    assert sum(1 for c in out if c.get("id") == "d1") == 1, "同 id 必须去重"


def test_union_arm_failure_degrades_to_dense():
    """关键词臂失败 → 降级稠密候选路径，检索绝不阻断。"""
    idx = _mk_indexer(_DENSE, [], keyword_raises=True)
    out = _run_swr(idx, ["告警"])
    assert [c.get("id") for c in out[:2]] and any(
        c.get("id") == "d1" for c in out), "关键词臂失败必须保住稠密结果"


def test_union_disabled_by_zero_cap():
    """hybrid_union_scroll_limit=0 → 关键词臂完全不调用（旧行为回退阀）。"""
    calls: list = []
    idx = _mk_indexer(_DENSE, [_KW_HIT], union_cap=0, calls=calls)
    out = _run_swr(idx, ["selectalarmtask"])
    assert calls == [], "旋钮为 0 时不得调用关键词臂"
    assert not any(c.get("id") == "k1" for c in out)


def test_union_cap_passed_as_scroll_limit():
    """配置的池上限必须真实传给关键词臂（否则默认 top_k*10 覆盖不足还无留痕）。"""
    calls: list = []
    idx = _mk_indexer(_DENSE, [_KW_HIT], union_cap=3333, calls=calls)
    _run_swr(idx, ["selectalarmtask"])
    assert calls and calls[0]["scroll_limit"] == 3333


def test_bm25_per_file_cap_diversifies():
    """R65B-T3 二阶段：每文件多样性上限——sql 全量 dump 等单文件几十个 chunk 霸榜
    会把其它文件的关键词命中整批挤出 top_k。cap 后同文件最多留 N 条（宁缺勿滥，
    不回填同文件溢出块）。"""
    import asyncio as _aio
    from unittest.mock import AsyncMock

    idx = object.__new__(SemanticIndexer)

    class _Cfg:
        retrieval_top_k = 5

    idx._kb_config = _Cfg()
    idx._collection_name = "test_kb"

    class _Pt:
        def __init__(self, i, fp, content):
            self.id = f"p{i}"
            self.payload = {"file_path": fp, "content": content,
                            "project_id": "p1"}

    # 6 个高分同文件块 + 1 个低分其它文件块
    pts = [_Pt(i, "sql/dump.sql", "告警 任务 调度 " * (10 - i)) for i in range(6)]
    pts.append(_Pt(9, "src/AlarmTask.java", "告警"))
    client = AsyncMock()
    client.scroll = AsyncMock(return_value=(pts, None))
    idx._client_or_raise = lambda: client

    out = _aio.run(idx.bm25_only_search(
        "p1", query_terms=["告警"], top_k=5, per_file_cap=3))
    by_file = {}
    for c in out:
        by_file[c["file_path"]] = by_file.get(c["file_path"], 0) + 1
    assert by_file.get("sql/dump.sql", 0) <= 3, f"单文件超上限: {by_file}"
    assert "src/AlarmTask.java" in by_file, "其它文件命中不应被同文件霸榜挤出"
