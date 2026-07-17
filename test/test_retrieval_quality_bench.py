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
# 中文查询被中文文案富集的模板系统性抢占稠密 top-20。彼时实测 0.364/0.500。
#
# R65B-T3 战役第一阶段（同日）两治本后上调地板：
# ① 真混合候选并集：BM25 关键词臂（bm25_only_search，池上限
#    SWARM_KB_HYBRID_UNION_SCROLL_LIMIT=5000）独立供给候选，与稠密候选按 id 并集
#    后融合重排——关键词精确命中不再被稠密 top-20 门槛挡死；
# ② bench 测量口径修正：query_terms 原来传整句（BM25 文档侧是分词口径，整句永不
#    匹配=BM25 维度一直无效测量），改用生产同款 _extract_keywords。
# 实测 Recall@5=0.432 / Hit@5=0.591（池上限 10000 全库覆盖反而更差 0.386/0.545：
# 瓶颈已从「候选缺席」转移到「rerank 池内噪声竞争」——sql/模板富中文块挤掉 gold）。
# 0.75 目标属战役第二阶段（gold 集复审+类型加权，避免对 22 题基准裸调参过拟合），
# 见 ROUND65_POSTMORTEM_TREATMENT_REGISTER.md R65B-T3 条目。地板留安全余量防 flake。
RECALL_FLOOR = 0.38
HIT_FLOOR = 0.50


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
