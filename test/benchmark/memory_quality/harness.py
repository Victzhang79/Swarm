"""编排 — 播种/老化/查询，把真实 store 行为喂给 metrics。

老化抽象 age_memory(store, days)：WS1 惰性衰减落地后 = 把 last_seen_at/last_used_at 回拨 days 天，
让 query 的 as_of=now() 看到真实年龄的 effective_weight（不再 tick 乘减 base）。

需要真实 PG(pgvector) + embedding 服务，遵循 retrieval_bench"无 mock、连真库"的约定。
合成模式仍写真实库（受控播种），只是数据是构造的。
"""

from __future__ import annotations

import asyncio
from typing import Any

from swarm.memory.consolidate import MemoryConsolidator
from swarm.memory.decay import MemoryDecay
from swarm.memory.store import MemoryStore, MistakeEntry, SuccessEntry

from golden_from_l2 import (
    GoldenSample,
    derive_golden_from_l2,
    synthetic_catalog,
    synthetic_probes,
    synthetic_samples,
)
from metrics import (
    ForgetCase,
    MemoryQualityReport,
    RecencyPair,
    aggregate_recall,
    dedup_rate,
    forgetting_accuracy,
    rank_of_first_relevant,
    recency_score,
)

# 合成主题里 fresh 条目的强化次数（occurrence/reuse 抬升 → 衰减更慢、模拟"新鲜活跃"）
_FRESH_REINFORCE = 5

# 去重探针：同一错误的 N 条近义碎片(模拟写时去重漏网，如 embed 挂时插重)，期望整合后坍缩成 1。
_DUP_N = 5
_DUP_ERROR_TYPE = "dup_probe"
_DUP_BASE = "用户对象 user 未判空就调用 user.getName() 抛出 NullPointerException 空指针异常"


async def age_memory(store: MemoryStore, days: float, project_id: str) -> None:
    """老化记忆 days 天 —— WS1 惰性模型：把 last_seen_at/last_used_at 回拨 days 天，
    让 query 的 as_of=now() 现算出真实年龄的 effective_weight（不再 tick 乘减 base）。"""
    conn = store._conn_or_raise()
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE mem_mistakes SET last_seen_at = last_seen_at - (%s * interval '1 day') "
            "WHERE project_id = %s",
            (float(days), project_id),
        )
        await cur.execute(
            "UPDATE mem_successes SET last_used_at = last_used_at - (%s * interval '1 day') "
            "WHERE project_id = %s",
            (float(days), project_id),
        )


async def _query_by(
    store: MemoryStore, project_id: str, sample: GoldenSample, top_k: int
) -> list[dict]:
    if sample.kind == "l6":
        return await store.query_successes(project_id, sample.query, top_k=top_k)
    return await store.query_mistakes(project_id, sample.query, top_k=top_k)


# ──────────────────────────────────────────────
# 合成流程：暴露"近因排序"与"遗忘正确性"
# ──────────────────────────────────────────────

async def _purge_project(store: MemoryStore, project_id: str) -> None:
    """清空该项目的 L5/L6，保证合成基线可重复运行（幂等）。"""
    conn = store._conn_or_raise()
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM mem_mistakes WHERE project_id = %s", (project_id,))
        await cur.execute("DELETE FROM mem_successes WHERE project_id = %s", (project_id,))


async def seed_synthetic(store: MemoryStore, project_id: str) -> dict[str, int]:
    """播种合成 catalog，返回 local_id → db_id 映射（全部以 L5 错题播种）。"""
    mapping: dict[str, int] = {}
    for e in synthetic_catalog():
        db_id = await store.write_mistake(
            project_id,
            MistakeEntry(
                error_type=e.error_type,
                description=e.text,
                context=None,
                fix_description=None,
                # 线上 mem_mistakes.task_id 为 NOT NULL（与 store.py 可空 DDL 漂移）→ 显式给值
                task_id=f"memq-{e.local_id}",
                metadata={"theme": e.theme, "synthetic": True, "local_id": e.local_id},
            ),
        )
        mapping[e.local_id] = db_id
    return mapping


async def _backdate_mistakes(
    store: MemoryStore, ids: list[int], days: float
) -> None:
    """把指定错题的 last_seen_at 回拨 days 天（选择性老化，仅对给定 id）。"""
    if not ids:
        return
    conn = store._conn_or_raise()
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE mem_mistakes SET last_seen_at = last_seen_at - (%s * interval '1 day') "
            "WHERE id = ANY(%s)",
            (float(days), list(ids)),
        )


async def _seed_dup_cluster(store: MemoryStore, project_id: str, n: int) -> int:
    """播种 n 条近义碎片(同一 error_type，措辞微调，cos≈1)，返回写入条数。模拟写时去重漏网。"""
    for i in range(n):
        # 仅末尾微调，保证 embedding 高度相似(cos≥θ)，触发整合
        text = f"{_DUP_BASE}（场景 {i + 1}）"
        await store.write_mistake(
            project_id,
            MistakeEntry(
                error_type=_DUP_ERROR_TYPE,
                description=text,
                task_id=f"memq-dup-{i}",
                metadata={"synthetic": True, "dup_cluster": True},
            ),
        )
    return n


async def _count_active(store: MemoryStore, project_id: str, error_type: str) -> int:
    """统计某 error_type 下未被 merged/archived/dismissed 的活跃条数。"""
    conn = store._conn_or_raise()
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM mem_mistakes WHERE project_id = %s AND error_type = %s "
            "AND COALESCE(metadata_json->>'status', '') NOT IN ('merged', 'archived', 'dismissed')",
            (project_id, error_type),
        )
        return int((await cur.fetchone())[0])


async def run_synthetic(
    store: MemoryStore, decay: MemoryDecay, project_id: str, *, k: int, age_days: float
) -> MemoryQualityReport:
    catalog = synthetic_catalog()
    await _purge_project(store, project_id)   # 幂等：清掉上次播种
    mapping = await seed_synthetic(store, project_id)

    # 主题分组
    themes: dict[str, dict[str, str]] = {}
    for e in catalog:
        if e.theme == "noise":
            continue
        themes.setdefault(e.theme, {})["fresh" if not e.is_stale else "stale"] = e.local_id
    noise_ids = [mapping[e.local_id] for e in catalog if e.theme == "noise"]
    stale_ids = [mapping[locs["stale"]] for locs in themes.values()]
    fresh_ids = [mapping[locs["fresh"]] for locs in themes.values()]

    # 强化 fresh → occurrence↑ 衰减更慢、last_seen_at 刷新到 now（真正“新鲜活跃”）
    for fid in fresh_ids:
        for _ in range(_FRESH_REINFORCE):
            await store.increment_mistake_occurrence(fid)

    # 近因阶段：把 stale【中度】老化(age_days/5，仍在阈值上、可被召回)，制造“新鲜 vs 较旧近义”落差。
    # 旧基线在此处两条都 age≈0 → 纯余弦无近因概念 → 0.333；WS2 近因融合排序应把 fresh 顶到 stale 前。
    recency_age = max(1.0, age_days / 5.0)
    await _backdate_mistakes(store, stale_ids, recency_age)

    pairs: list[RecencyPair] = []
    probes = synthetic_probes()
    for theme, locs in themes.items():
        res = await store.query_mistakes(project_id, probes[theme], top_k=k)
        ids = [r.get("id") for r in res]
        fr = (ids.index(mapping[locs["fresh"]]) + 1) if mapping[locs["fresh"]] in ids else 0
        sr = (ids.index(mapping[locs["stale"]]) + 1) if mapping[locs["stale"]] in ids else 0
        pairs.append(RecencyPair(fresh_rank=fr, stale_rank=sr))

    # 遗忘阶段：把 stale + noise 再重度老化(总年龄越过阈值)，fresh 不动（保持新鲜存活）。
    await _backdate_mistakes(store, stale_ids + noise_ids, age_days)

    # POST-AGING：有效权重快照(读时现算) + 召回
    alive = await store.get_all_mistakes(project_id, min_weight=0.0)
    weight_by_id = {row["id"]: row["effective_weight"] for row in alive}

    samples = synthetic_samples()
    per_query: list[tuple[list[dict], set]] = []
    forget_cases: list[ForgetCase] = []
    seen_ids: set = set()
    for s in samples:
        rel = {mapping[lid] for lid in s.relevant_ids if lid in mapping}
        res = await store.query_mistakes(project_id, s.query, top_k=k)
        per_query.append((res, rel))
        seen_ids.update(r.get("id") for r in res)

    for e in catalog:
        db_id = mapping[e.local_id]
        forget_cases.append(ForgetCase(
            id=db_id,
            expected_forgotten=e.is_stale,            # 陈旧/噪声=应遗忘，fresh=应保留
            effective_weight=float(weight_by_id.get(db_id, 0.0)),
            in_results=db_id in seen_ids,
        ))

    recall = aggregate_recall(per_query, k)

    # 去重阶段(WS3)：播种 N 条近义碎片 → 批量整合 → 量坍缩程度。放最后，不扰动前面度量。
    written = await _seed_dup_cluster(store, project_id, _DUP_N)
    consolidator = MemoryConsolidator(store)
    await consolidator.consolidate_mistakes(project_id)
    distinct_after = await _count_active(store, project_id, _DUP_ERROR_TYPE)
    dedup = dedup_rate(written, distinct_after)

    return MemoryQualityReport(
        config={"mode": "synthetic", "k": k, "age_days": age_days,
                "recency_age": recency_age, "project_id": project_id},
        recall=recall.__dict__,
        forgetting_accuracy=forgetting_accuracy(forget_cases),
        recency_score=recency_score(pairs),
        dedup_rate=dedup,
        notes=[
            "recency(WS2)：stale 中度老化后，近因融合排序 rank=sim*(FLOOR+(1-FLOOR)*recency_ratio) "
            "把新鲜同义顶到陈旧近义之前 → recency_score 应从 0.333 提升",
            "forgetting(WS1 惰性)：stale+noise 越过阈值被遗忘，fresh(occurrence↑+新鲜)存活",
            f"dedup(WS3)：{written} 条近义碎片整合后剩 {distinct_after} 条活跃 → dedup_rate={dedup:.3f}",
        ],
    )


# ──────────────────────────────────────────────
# 真实流程：L2 派生召回（遗忘/近因需受控时间，WS1 as_of 落地后补）
# ──────────────────────────────────────────────

async def run_real(
    store: MemoryStore, project_id: str, *, k: int, limit: int = 50
) -> MemoryQualityReport:
    samples = await derive_golden_from_l2(store, project_id, limit=limit)
    per_query: list[tuple[list[dict], set]] = []
    for s in samples:
        rel = set(s.relevant_ids)
        res = await _query_by(store, project_id, s, top_k=k)
        per_query.append((res, rel))
    recall = aggregate_recall(per_query, k)
    return MemoryQualityReport(
        config={"mode": "real", "k": k, "project_id": project_id, "n_samples": len(samples)},
        recall=recall.__dict__,
        forgetting_accuracy=float("nan"),  # 需受控时间轴，WS1 as_of 后启用
        recency_score=float("nan"),
        dedup_rate=0.0,
        notes=[
            "real：召回=查 L2 摘要能否召回其直链 L5/L6 条目",
            "遗忘/近因在真实数据上需 WS1 的 as_of 时间旅行后才可度量",
        ],
    )


def _ranks_debug(results: list[dict], relevant: set) -> int:  # 小工具，便于排查
    return rank_of_first_relevant(results, relevant)


def connect_pair() -> tuple[MemoryStore, MemoryDecay]:
    """构造未连接的 store + decay（调用方负责 await store.connect()）。"""
    store = MemoryStore()
    decay = MemoryDecay(store)
    return store, decay


async def aconnect() -> tuple[MemoryStore, MemoryDecay]:
    store, decay = connect_pair()
    await store.connect()
    return store, decay
