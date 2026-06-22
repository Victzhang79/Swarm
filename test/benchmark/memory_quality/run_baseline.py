"""跑记忆质量基线 → scorecard + baseline.json（镜像 retrieval_bench main）。

用法(按文件路径运行，与 retrieval_bench 一致；避免 test 包名与 stdlib 冲突):
  .venv/bin/python test/benchmark/memory_quality/run_baseline.py --synthetic --report baseline.json
  .venv/bin/python test/benchmark/memory_quality/run_baseline.py --project <pid> --k 5 --report baseline.json

合成模式自给数据(受控播种到真库)，最稳，先用它抓"近因/遗忘"基线；
真实模式从该项目 L2 派生召回样本（需该项目已有任务历史）。
两种模式都需 PG(pgvector) + embedding 服务可用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import asdict

from harness import aconnect, run_real, run_synthetic
from metrics import MemoryQualityReport

# RuoYi-E2E 基线项目（与 retrieval_bench 一致）
DEFAULT_PROJECT = "5d0e9db8-d000-40f6-8df9-a929ea3c4712"
# 合成模式独立 project，避免污染真实项目记忆
SYNTHETIC_PROJECT = "memq-synthetic-bench"


def _fmt(x: float) -> str:
    return "nan" if isinstance(x, float) and math.isnan(x) else f"{x:.3f}"


def format_scorecard(rep: MemoryQualityReport) -> str:
    r = rep.recall
    c = rep.config
    lines = [
        "", "=" * 72,
        "记忆质量离线基线 (L5/L6)",
        "=" * 72,
        f"config: {c}",
        "-" * 72,
        f"召回  Hit@{c.get('k')}     = {_fmt(r['hit_at_k'])}",
        f"召回  MRR        = {_fmt(r['mrr'])}",
        f"召回  Recall@k   = {_fmt(r['recall_at_k'])}",
        f"召回  Precision@k= {_fmt(r['precision_at_k'])}",
        f"遗忘正确性       = {_fmt(rep.forgetting_accuracy)}",
        f"近因排序         = {_fmt(rep.recency_score)}",
        f"去重率(WS3占位)  = {_fmt(rep.dedup_rate)}",
        "-" * 72,
    ]
    lines += [f"note: {n}" for n in rep.notes]
    lines.append("=" * 72)
    return "\n".join(lines)


async def _run(args) -> MemoryQualityReport:
    store, decay = await aconnect()
    try:
        if args.synthetic:
            return await run_synthetic(
                store, decay, SYNTHETIC_PROJECT, k=args.k, age_days=args.age_days
            )
        return await run_real(store, args.project, k=args.k, limit=args.limit)
    finally:
        await store.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="记忆质量离线基线")
    ap.add_argument("--synthetic", action="store_true", help="用合成集(受控播种)而非真实 L2")
    ap.add_argument("--project", type=str, default=DEFAULT_PROJECT)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--age_days", type=float, default=60.0, help="合成模式老化天数(tick 数)")
    ap.add_argument("--limit", type=int, default=50, help="真实模式 L2 取样上限")
    ap.add_argument("--report", type=str, default=None)
    args = ap.parse_args()

    rep = asyncio.run(_run(args))
    print(format_scorecard(rep))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(asdict(rep), fh, ensure_ascii=False, indent=2)
        print(f"\nreport 已写入: {args.report}")


if __name__ == "__main__":
    main()
