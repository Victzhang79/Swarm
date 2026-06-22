"""记忆质量指标 — 纯函数，不依赖 DB / 网络，可独立单测。

度量四类(对齐计划 WS0):
  1. 召回质量      recall@k / precision@k / MRR（按 entry id 判命中，比检索 bench 的子句判定更精确）
  2. 遗忘正确性    被标"应遗忘"的陈旧条目，老化后是否确实沉到阈值下/不再召回
  3. 近因排序      成对样本中"新鲜相关"是否排在"陈旧近义"之前
  4. 去重率        占位（WS3 填）—— 同簇近义条目折叠程度

判命中以条目 id 集合为准：检索返回 [{"id": ..., ...}, ...]，relevant 为 id 集合。
这比检索 bench 的 source/keyword 子句判定更干净，因为记忆条目有稳定主键。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

DEFAULT_FORGET_THRESHOLD = 0.05


# ──────────────────────────────────────────────
# 单题原子指标（按 entry id）
# ──────────────────────────────────────────────

def rank_of_first_relevant(results: list[dict], relevant_ids: set) -> int:
    """第一条命中 relevant 的 1-based 排名；无命中返回 0（对齐 retrieval_bench 语义）。"""
    for idx, r in enumerate(results, start=1):
        if r.get("id") in relevant_ids:
            return idx
    return 0


def recall_at_k(results: list[dict], relevant_ids: set, k: int) -> float:
    """top-k 覆盖的 relevant 比例。relevant 为空返回 0（无可召回视为不计分）。"""
    if not relevant_ids:
        return 0.0
    got = {r.get("id") for r in results[:k]}
    return len(got & relevant_ids) / len(relevant_ids)


def precision_at_k(results: list[dict], relevant_ids: set, k: int) -> float:
    """top-k 中 relevant 的占比。返回结果为空返回 0。"""
    topk = results[:k]
    if not topk:
        return 0.0
    got_rel = sum(1 for r in topk if r.get("id") in relevant_ids)
    return got_rel / len(topk)


# ──────────────────────────────────────────────
# 遗忘正确性
# ──────────────────────────────────────────────

@dataclass
class ForgetCase:
    """一条用于验证遗忘的条目。

    expected_forgotten: 真值标签——该条目老化后是否【应当】被遗忘（陈旧且无关）。
    effective_weight:   老化后的有效权重（当前实现取 decay_weight）。
    in_results:         老化后是否仍出现在召回结果里。
    """
    id: int | str
    expected_forgotten: bool
    effective_weight: float
    in_results: bool


def is_forgotten(case: ForgetCase, threshold: float = DEFAULT_FORGET_THRESHOLD) -> bool:
    """判定一条目实际是否已被遗忘：权重沉到阈值下【或】已不在召回结果中。"""
    return case.effective_weight <= threshold or not case.in_results


def forgetting_accuracy(
    cases: list[ForgetCase], threshold: float = DEFAULT_FORGET_THRESHOLD
) -> float:
    """遗忘正确性 = 预测(实际是否遗忘)与真值标签一致的比例。"""
    if not cases:
        return 0.0
    correct = sum(
        1 for c in cases if is_forgotten(c, threshold) == c.expected_forgotten
    )
    return correct / len(cases)


# ──────────────────────────────────────────────
# 近因排序
# ──────────────────────────────────────────────

@dataclass
class RecencyPair:
    """近因成对样本：同主题下一条新鲜相关、一条陈旧近义，期望新鲜排在前。"""
    fresh_rank: int   # 0 = 未召回
    stale_rank: int   # 0 = 未召回


def recency_ordering_correct(pair: RecencyPair) -> bool:
    """新鲜相关是否正确排在陈旧近义之前。

    - 新鲜未召回 → 错（最坏：该出现的没出现）。
    - 新鲜召回、陈旧未召回 → 对（陈旧被正确压下/过滤）。
    - 两者都召回 → 新鲜 rank 更靠前(数值更小)才算对。
    """
    if pair.fresh_rank == 0:
        return False
    if pair.stale_rank == 0:
        return True
    return pair.fresh_rank < pair.stale_rank


def recency_score(pairs: list[RecencyPair]) -> float:
    if not pairs:
        return 0.0
    return sum(1 for p in pairs if recency_ordering_correct(p)) / len(pairs)


# ──────────────────────────────────────────────
# 去重率（WS3 占位）
# ──────────────────────────────────────────────

def dedup_rate(total_written: int, distinct_after_dedup: int) -> float:
    """去重率 = 1 - 去重后条数/写入尝试次数。total_written<=0 返回 0。"""
    if total_written <= 0:
        return 0.0
    return max(0.0, 1.0 - distinct_after_dedup / total_written)


# ──────────────────────────────────────────────
# 聚合报告
# ──────────────────────────────────────────────

@dataclass
class RecallReport:
    n: int
    hit_at_k: float
    mrr: float
    recall_at_k: float
    precision_at_k: float


def aggregate_recall(
    per_query: list[tuple[list[dict], set]], k: int
) -> RecallReport:
    """聚合多题召回指标。per_query: [(results, relevant_ids), ...]。"""
    n = len(per_query)
    if n == 0:
        return RecallReport(0, 0.0, 0.0, 0.0, 0.0)
    ranks = [rank_of_first_relevant(res, rel) for res, rel in per_query]
    hit = sum(1 for r in ranks if r > 0) / n
    mrr = sum((1.0 / r) for r in ranks if r) / n
    rec = sum(recall_at_k(res, rel, k) for res, rel in per_query) / n
    prec = sum(precision_at_k(res, rel, k) for res, rel in per_query) / n
    return RecallReport(n=n, hit_at_k=hit, mrr=mrr, recall_at_k=rec, precision_at_k=prec)


@dataclass
class MemoryQualityReport:
    config: dict
    recall: dict
    forgetting_accuracy: float
    recency_score: float
    dedup_rate: float
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)
