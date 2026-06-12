"""混合检索融合 — 向量分数 + BM25 关键词分数加权融合。

为什么：纯向量检索对【精确符号名/术语/标识符】召回弱（向量把 isBlank 和
"判断空" 拉近，但对精确的 `selectUserByLoginName` 这类不如关键词匹配准）。
BM25 关键词分对精确匹配强、对语义弱。两者融合(hybrid)取长补短。

做法（轻量、无需独立 BM25 索引）：
- 向量召回已有候选(含 content + score)。
- 用任务关键词(retriever._extract_keywords，含中文 2-gram)对候选 content 算
  BM25 分(标准 IDF*TF 饱和公式)。
- 归一化两路分数 → final = (1-w)*vec + w*bm25，w = hybrid_bm25_weight。
- 返回按 final 降序的候选，再交给 reranker 精排。

注：这是"召回后重打分"的 hybrid，不改 Qdrant 查询；候选集足够大(retrieval_top_k)
时效果接近真混合检索，且零额外远端调用。
"""

from __future__ import annotations

import math
from collections import Counter


def _bm25_scores(
    query_terms: list[str],
    docs_terms: list[list[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """标准 BM25 打分。query_terms/docs_terms 已分词、小写。返回每文档分数。"""
    n = len(docs_terms)
    if n == 0 or not query_terms:
        return [0.0] * n
    doc_len = [len(d) for d in docs_terms]
    avgdl = (sum(doc_len) / n) or 1.0
    # 文档频率
    df: Counter[str] = Counter()
    for d in docs_terms:
        for t in set(d):
            df[t] += 1
    scores = [0.0] * n
    q_set = set(query_terms)
    for i, d in enumerate(docs_terms):
        if not d:
            continue
        tf = Counter(d)
        s = 0.0
        for t in q_set:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            freq = tf[t]
            s += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * doc_len[i] / avgdl))
        scores[i] = s
    return scores


def _tokenize_doc(text: str) -> list[str]:
    """文档分词：英文小写 token + 中文 2-gram（与 query 关键词口径一致）。"""
    import re
    if not text:
        return []
    out: list[str] = []
    # 英文/数字/下划线 token
    for tok in re.findall(r"[A-Za-z0-9_]+", text.lower()):
        if len(tok) >= 2:
            out.append(tok)
        # 下划线/驼峰拆分
        for part in re.split(r"[_]", tok):
            if len(part) >= 2 and part != tok:
                out.append(part)
    # 中文 2-gram
    for cn in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(cn) - 1):
            out.append(cn[i : i + 2])
    return out


def _normalize(scores: list[float]) -> list[float]:
    """min-max 归一化到 [0,1]；全相等则全 0。"""
    if not scores:
        return scores
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [0.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


def hybrid_fuse(
    candidates: list[dict],
    query_terms: list[str],
    *,
    bm25_weight: float = 0.3,
    text_key: str = "content",
) -> list[dict]:
    """对向量召回候选做 BM25 融合重打分，返回按融合分降序的新列表。

    candidates: [{content, score(向量分), ...}]
    query_terms: 已提取的查询关键词（英文 token + 中文 2-gram）
    bm25_weight: 0=纯向量，1=纯关键词
    每个候选写入 hybrid_score / bm25_score 字段（便于调试/阈值）。
    """
    if not candidates:
        return candidates
    w = max(0.0, min(float(bm25_weight), 1.0))
    if w <= 0.0 or not query_terms:
        return candidates  # 纯向量，原样返回

    vec_scores = [float(c.get("score", 0.0) or 0.0) for c in candidates]
    docs_terms = [_tokenize_doc(str(c.get(text_key) or c.get("text") or "")) for c in candidates]
    bm25 = _bm25_scores([t.lower() for t in query_terms], docs_terms)

    vec_n = _normalize(vec_scores)
    bm25_n = _normalize(bm25)

    fused = []
    for i, c in enumerate(candidates):
        c = dict(c)
        c["bm25_score"] = round(bm25[i], 4)
        c["hybrid_score"] = round((1 - w) * vec_n[i] + w * bm25_n[i], 4)
        fused.append(c)
    fused.sort(key=lambda x: x["hybrid_score"], reverse=True)
    return fused
