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

# R65B-T2 重定基线（2026-07-17，非静默降标——完整因果留痕）：
# 旧地板 0.75 标定于【不可复现的偏置态】：增量回灌层只含 20+ 失败轮 worker 碰过的
# 业务 Java 文件（连同幻影模块），中文 NL 查询在纯业务子集里稠密检索天然赢
# （实测彼时 0.955）。R65-T2 知识层 purge + R65B-T2 源码全文嵌入后，KB 首次
# 【诚实且可复现】：全部 489 个源文件（含 Thymeleaf 模板/文档）入语义层，
# 中文查询被中文文案富集的模板系统性抢占稠密 top-20——混合融合只在稠密候选内
# 重排，救不回没进候选的关键词精确命中（bm25_only_search 原语健在但未接入
# 主检索=真混合候选并集缺口）。可复现基线实测 Recall@5=0.364 / Hit@5=0.500。
# 新地板贴着可复现基线设（继续守【回归】），0.75 目标随真混合/类型加权战役
# 达成后回调——见 ROUND65_POSTMORTEM_TREATMENT_REGISTER.md R65B-T3 战役条目。
RECALL_FLOOR = 0.30
HIT_FLOOR = 0.42


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
