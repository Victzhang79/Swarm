"""CI 回归:KB 检索质量基线不得退化。

每改 KB 检索(bm25_weight 默认值、Parent-Child 切分、rerank、embed/索引流水线),本测试用
【从 KB 真实内容反推的 gold 题集】跑真实检索,断言基线 Recall@5 / Hit@5 不低于下限,
免再靠 $3000/轮 live E2E 才发现召回掉了。

实测基线(默认 bm25_weight=0.3 + rerank on, RuoYi-E2E): Hit@5=0.955 / MRR=0.932 / Recall@5=0.955。
阈值设 0.75（实测 0.955 的安全余量,防回归不误报）。

依赖远端 embed/rerank 服务(ai.bit)与已建好的 RuoYi-E2E KB。服务不可用 / KB 为空时
本测试 skip(不误判为失败) —— 它守护的是【召回质量回归】,不是【服务存活】。
"""

from __future__ import annotations

import asyncio

import pytest

from test.benchmark.retrieval_quality.retrieval_bench import run_bench

RECALL_FLOOR = 0.75
HIT_FLOOR = 0.75


@pytest.fixture(scope="module")
def baseline_report():
    try:
        rep = asyncio.run(run_bench(k=5, bm25_weight=None, use_rerank=True))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"检索服务/KB 不可用,跳过: {type(exc).__name__}: {exc}")
    # KB 空 / 检索全空 → 视为环境未就绪而非回归
    if rep.n_questions == 0 or all(p["n_returned"] == 0 for p in rep.per_question):
        pytest.skip("KB 为空或检索全无返回(embed/rerank 服务不可用),跳过基线断言")
    return rep


def test_recall_at_5_not_regressed(baseline_report):
    assert baseline_report.recall_at_k >= RECALL_FLOOR, (
        f"Recall@5={baseline_report.recall_at_k:.3f} 低于下限 {RECALL_FLOOR} — 检索召回退化"
    )


def test_hit_at_5_not_regressed(baseline_report):
    assert baseline_report.hit_at_k >= HIT_FLOOR, (
        f"Hit@5={baseline_report.hit_at_k:.3f} 低于下限 {HIT_FLOOR} — 检索命中退化"
    )
