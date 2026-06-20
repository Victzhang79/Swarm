"""检索质量离线评测基准（Phase 0 — 只读，零生产代码改动）。

痛点:调 KB 检索参数(bm25_weight、Parent-Child 切分、rerank 开关)时没有秒级量化依据,
只能盲调或靠 $3000/轮 的 live E2E 撞效果。本基准用【从 KB 真实内容反推的 gold 题集】重放
真实检索(SemanticIndexer.search_with_rerank),算 Hit@K / MRR / Recall@K,把"调参有没有变好"
变成一张可对比的打分表。

命中判定（参考 plan_quality 的 score_hits 思路 + relevant_groups 等价源）:
  每道题的 relevant 是一组 OR 子句(relevant_groups),任一返回结果满足任一子句即算【命中该题】。
  单个子句满足条件: 返回结果的 file_path/source 含 source_contains 【且/或】 content 含 keyword_contains。
  （子句内 source_contains 与 keyword_contains 都给则需同时满足;只给一个则只判一个。）

用法:
    .venv/bin/python test/benchmark/retrieval_quality/retrieval_bench.py            # 默认配置基线
    .venv/bin/python test/benchmark/retrieval_quality/retrieval_bench.py --k 5
    .venv/bin/python test/benchmark/retrieval_quality/retrieval_bench.py --bm25_weight 0.0   # 纯向量
    .venv/bin/python test/benchmark/retrieval_quality/retrieval_bench.py --no-rerank
    .venv/bin/python test/benchmark/retrieval_quality/retrieval_bench.py --report out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass, field

from swarm.config.settings import get_config
from swarm.knowledge.semantic_index import SemanticIndexer

# RuoYi-E2E 基线项目（与 plan_quality / E2E 一致）
PROJECT_ID = "5d0e9db8-d000-40f6-8df9-a929ea3c4712"

_HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(_HERE, "fixtures", "ruoyi_retrieval.jsonl")


def load_questions(path: str = FIXTURE) -> list[dict]:
    qs: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                qs.append(json.loads(line))
    return qs


def _clause_hit(result: dict, clause: dict) -> bool:
    """单个 relevant 子句是否被一条返回结果满足。"""
    sc = clause.get("source_contains")
    kw = clause.get("keyword_contains")
    ok = True
    if sc:
        # source_contains 同时匹配 file_path 与 source（语义结果两者其一带定位信息）
        hay = f"{result.get('file_path') or ''}\n{result.get('source') or ''}\n{result.get('name') or ''}\n{result.get('class_name') or ''}"
        ok = ok and (sc in hay)
    if kw:
        content = result.get("content") or ""
        title = result.get("title") or ""  # norms 用 title，语义用 content
        ok = ok and (kw in content or kw in title)
    return ok


def _rank_of_first_relevant(results: list[dict], groups: list[dict]) -> int:
    """返回第一条命中【任一 relevant 子句】的结果的 1-based 排名;无命中返回 0。"""
    for idx, r in enumerate(results, start=1):
        for clause in groups:
            if _clause_hit(r, clause):
                return idx
    return 0


@dataclass
class QResult:
    id: str
    cat: str
    query: str
    hit: bool
    first_rank: int          # 0 = miss
    n_returned: int
    n_clauses: int
    n_clauses_hit: int       # 多少个 relevant 子句在 top-k 中被覆盖(Recall 用)


@dataclass
class BenchReport:
    config: dict
    n_questions: int
    hit_at_k: float
    mrr: float
    recall_at_k: float       # 平均: 每题被覆盖的 relevant 子句比例
    per_question: list[dict] = field(default_factory=list)


async def run_bench(
    *, k: int, bm25_weight: float | None, use_rerank: bool,
    retrieval_top_k: int | None = None,
) -> BenchReport:
    cfg = get_config()
    kb = cfg.knowledge

    # ── 仅在内存覆盖配置(绝不落库)──
    if bm25_weight is not None:
        kb.hybrid_bm25_weight = bm25_weight
    eff_bm25 = kb.hybrid_bm25_weight
    retrieval_top_k = retrieval_top_k or kb.retrieval_top_k

    sem = SemanticIndexer(cfg.db, kb)
    await sem.connect()

    questions = load_questions()
    per: list[QResult] = []
    try:
        for q in questions:
            groups = q.get("relevant") or []
            if use_rerank:
                res = await sem.search_with_rerank(
                    PROJECT_ID, q["query"],
                    retrieval_top_k=retrieval_top_k,
                    rerank_top_k=k,
                    query_terms=[q["query"]],
                )
            else:
                # --no-rerank: 纯向量(可选 BM25 融合)召回,不过 reranker,取前 k
                res = await sem.search(PROJECT_ID, q["query"], top_k=retrieval_top_k)
                if eff_bm25 > 0 and res:
                    try:
                        from swarm.knowledge.hybrid import hybrid_fuse
                        res = hybrid_fuse(res, [q["query"]], bm25_weight=eff_bm25, text_key="content")
                    except Exception:  # noqa: BLE001
                        pass
                res = res[:k]

            rank = _rank_of_first_relevant(res, groups)
            n_hit_clauses = sum(
                1 for c in groups if any(_clause_hit(r, c) for r in res)
            )
            per.append(QResult(
                id=q["id"], cat=q.get("cat", "?"), query=q["query"],
                hit=rank > 0, first_rank=rank, n_returned=len(res),
                n_clauses=len(groups), n_clauses_hit=n_hit_clauses,
            ))
    finally:
        await sem.close()

    n = len(per)
    hit_at_k = sum(1 for p in per if p.hit) / n if n else 0.0
    mrr = sum((1.0 / p.first_rank) for p in per if p.first_rank) / n if n else 0.0
    recall_at_k = (
        sum((p.n_clauses_hit / p.n_clauses) for p in per if p.n_clauses) / n
        if n else 0.0
    )

    return BenchReport(
        config={
            "k": k, "bm25_weight": eff_bm25, "rerank": use_rerank,
            "retrieval_top_k": retrieval_top_k, "project_id": PROJECT_ID,
        },
        n_questions=n, hit_at_k=hit_at_k, mrr=mrr, recall_at_k=recall_at_k,
        per_question=[asdict(p) for p in per],
    )


def format_scorecard(rep: BenchReport) -> str:
    c = rep.config
    lines = [
        "", "=" * 78,
        "检索质量离线评测基准 (RuoYi-E2E)",
        "=" * 78,
        f"config: k={c['k']} bm25_weight={c['bm25_weight']} rerank={c['rerank']} "
        f"retrieval_top_k={c['retrieval_top_k']}",
        f"题数={rep.n_questions}",
        "-" * 78,
    ]
    for p in rep.per_question:
        mark = "OK " if p["hit"] else "MISS"
        lines.append(
            f"[{mark}] {p['id']:5} {p['cat']:11} rank={p['first_rank']} "
            f"clauses={p['n_clauses_hit']}/{p['n_clauses']}  {p['query'][:34]}"
        )
    lines += [
        "-" * 78,
        f"Hit@{c['k']}    = {rep.hit_at_k:.3f}",
        f"MRR       = {rep.mrr:.3f}",
        f"Recall@{c['k']} = {rep.recall_at_k:.3f}",
        "=" * 78,
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="KB 检索质量离线评测")
    ap.add_argument("--k", type=int, default=5, help="top-k (默认 5)")
    ap.add_argument("--bm25_weight", type=float, default=None,
                    help="临时覆盖 hybrid_bm25_weight(仅内存,不落库)")
    ap.add_argument("--rerank", dest="rerank", action="store_true", default=True)
    ap.add_argument("--no-rerank", dest="rerank", action="store_false")
    ap.add_argument("--retrieval_top_k", type=int, default=None)
    ap.add_argument("--report", type=str, default=None, help="写 report json 到此路径")
    args = ap.parse_args()

    rep = asyncio.run(run_bench(
        k=args.k, bm25_weight=args.bm25_weight, use_rerank=args.rerank,
        retrieval_top_k=args.retrieval_top_k,
    ))
    print(format_scorecard(rep))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(asdict(rep), fh, ensure_ascii=False, indent=2)
        print(f"\nreport 已写入: {args.report}")


if __name__ == "__main__":
    main()
