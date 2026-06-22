"""WS0 记忆质量基准 — 纯逻辑单测（不连 DB / 不连网络）。

验证 metrics 指标与 golden_from_l2 派生逻辑；用内存假 store 替代 PG。
保证 pytest 全量可跑、harness 逻辑正确，真实基线另由 run_baseline 连真库执行。
"""

from __future__ import annotations

import asyncio
import os
import sys

# benchmark 按文件路径运行(与 retrieval_bench 一致)，模块是顶层名而非 test.* 子包
# (test 与 stdlib 同名会冲突)。这里把基准目录挂上 sys.path 后按顶层导入。
_BENCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benchmark", "memory_quality")
if _BENCH_DIR not in sys.path:
    sys.path.insert(0, _BENCH_DIR)

from golden_from_l2 import (  # noqa: E402
    derive_golden_from_l2,
    synthetic_catalog,
    synthetic_samples,
)
from metrics import (  # noqa: E402
    ForgetCase,
    RecencyPair,
    aggregate_recall,
    dedup_rate,
    forgetting_accuracy,
    precision_at_k,
    rank_of_first_relevant,
    recall_at_k,
    recency_ordering_correct,
    recency_score,
)


# ── 召回原子指标 ──────────────────────────────

def _res(*ids):
    return [{"id": i} for i in ids]


def test_rank_of_first_relevant():
    assert rank_of_first_relevant(_res(9, 3, 7), {7}) == 3
    assert rank_of_first_relevant(_res(9, 3, 7), {3, 9}) == 1
    assert rank_of_first_relevant(_res(9, 3, 7), {42}) == 0


def test_recall_and_precision_at_k():
    res = _res(1, 2, 3, 4)
    assert recall_at_k(res, {2, 4, 99}, k=4) == 2 / 3
    assert recall_at_k(res, set(), k=4) == 0.0           # 无可召回不计分
    assert precision_at_k(res, {1, 2}, k=4) == 0.5
    assert precision_at_k([], {1}, k=4) == 0.0


def test_aggregate_recall():
    per = [(_res(1, 2), {1}), (_res(5, 6), {7})]          # 第一题命中 rank1，第二题 miss
    rep = aggregate_recall(per, k=2)
    assert rep.n == 2
    assert rep.hit_at_k == 0.5
    assert rep.mrr == 0.5                                  # (1/1 + 0)/2


# ── 遗忘正确性 ────────────────────────────────

def test_forgetting_accuracy():
    cases = [
        ForgetCase("a", expected_forgotten=True, effective_weight=0.01, in_results=False),  # 对
        ForgetCase("b", expected_forgotten=True, effective_weight=0.9, in_results=True),     # 错(该忘没忘)
        ForgetCase("c", expected_forgotten=False, effective_weight=0.8, in_results=True),    # 对
        ForgetCase("d", expected_forgotten=False, effective_weight=0.02, in_results=False),  # 错(误删)
    ]
    assert forgetting_accuracy(cases) == 0.5
    assert forgetting_accuracy([]) == 0.0


# ── 近因排序 ──────────────────────────────────

def test_recency_ordering():
    assert recency_ordering_correct(RecencyPair(fresh_rank=1, stale_rank=3)) is True
    assert recency_ordering_correct(RecencyPair(fresh_rank=3, stale_rank=1)) is False
    assert recency_ordering_correct(RecencyPair(fresh_rank=0, stale_rank=2)) is False  # 新鲜没召回
    assert recency_ordering_correct(RecencyPair(fresh_rank=2, stale_rank=0)) is True   # 陈旧被压下
    assert recency_score([RecencyPair(1, 2), RecencyPair(2, 1)]) == 0.5


def test_dedup_rate():
    assert dedup_rate(10, 4) == 0.6
    assert dedup_rate(0, 0) == 0.0
    assert dedup_rate(5, 8) == 0.0   # 不为负


# ── WS3 整合：并查集聚簇 + 代表选取（纯逻辑，不连库）──────────

def test_cluster_pairs_transitive():
    from swarm.memory.consolidate import cluster_pairs
    # 1-2, 2-3 传递成一簇 {1,2,3}；4-5 独立成簇；6 孤立不成簇
    pairs = [(1, 2), (2, 3), (4, 5)]
    nodes = {1, 2, 3, 4, 5, 6}
    clusters = sorted(cluster_pairs(pairs, nodes))
    assert clusters == [[1, 2, 3], [4, 5]]


def test_cluster_pairs_empty():
    from swarm.memory.consolidate import cluster_pairs
    assert cluster_pairs([], {1, 2, 3}) == []


def test_pick_representative_by_count_then_recency_then_id():
    from swarm.memory.consolidate import pick_representative
    # count 最高者胜
    rows = {1: {"count": 2, "last": 100}, 2: {"count": 9, "last": 1}, 3: {"count": 5, "last": 50}}
    assert pick_representative(rows) == 2
    # count 并列 → last 最新者胜
    rows = {1: {"count": 3, "last": 100}, 2: {"count": 3, "last": 300}}
    assert pick_representative(rows) == 2
    # count、last 全并列 → id 最小者胜
    rows = {7: {"count": 3, "last": 100}, 4: {"count": 3, "last": 100}}
    assert pick_representative(rows) == 4


# ── 合成集结构 ────────────────────────────────

def test_synthetic_catalog_pairs():
    cat = synthetic_catalog()
    themes = {e.theme for e in cat if e.theme != "noise"}
    for t in themes:
        kinds = {e.is_stale for e in cat if e.theme == t}
        assert kinds == {True, False}, f"主题 {t} 应有 fresh+stale 成对"
    # 合成召回样本只用 fresh、不含 noise
    samples = synthetic_samples()
    assert samples and all(s.source == "synthetic" for s in samples)
    assert all("noise" not in s.id for s in samples)


# ── 真实 L2 派生（内存假 store）────────────────

class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    async def query_task_summaries(self, project_id, limit=50):
        return self._rows[:limit]


def test_derive_golden_from_l2():
    rows = [
        {"summary": "给用户列表加排序", "metadata": {"success_id": 11, "modules": ["a/b"]}},
        {"summary": "登录 token 过期 500", "metadata": {"mistake_id": 22, "modules": ["c/d"]}},
        {"summary": "", "metadata": {"mistake_id": 33}},          # 空 query 跳过
        {"summary": "无直链摘要", "metadata": {"modules": ["x/y"]}},  # 无 id 跳过
    ]
    samples = asyncio.run(derive_golden_from_l2(_FakeStore(rows), "p", limit=50))
    by_id = {s.id: s for s in samples}
    assert by_id["l6-0"].relevant_ids == [11]
    assert by_id["l6-0"].kind == "l6"
    assert by_id["l6-0"].relevant_modules == ["a/b"]
    assert by_id["l5-1"].relevant_ids == [22]
    assert by_id["l5-1"].kind == "l5"
    assert len(samples) == 2   # 仅两条带直链的进集
