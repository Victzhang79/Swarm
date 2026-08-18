"""A5-M2 回归：`purge_expired` 必须留存带 status 标记的行（dismissed / merged）。

治前两条 DELETE 的 WHERE 只有 `effective_weight < 阈值`，无任何 status 谓词，而：
  · `dismiss_mistake`(store.py:673) 同时置 `decay_weight=0` ⇒ effective 恒 0 < 0.05
    ⇒ 人工裁决痕迹在**次日 03:00 那一轮**（api/app.py:1250 起的 start_daily_decay）必被物理删；
  · `consolidate`(consolidate.py:186-191) 标 merged 时只改 metadata、不动 base
    ⇒ 随年龄衰减后【最终】被删，而 merged 是 `get_memory_health` 的 dedup_rate **分母**
    ⇒ 指标单调漂向 0（越去重看着越像没去重）。

★本文件的区分力核心＝`test_plain_expired_row_is_still_deleted`★：缺它则"谓词写成恒假、
什么都不删"也能让其余用例全绿。两张表各有独立用例（治法改一条 DELETE ＝半落地）。

触真实 PG，`_test_` 前缀 project_id 隔离 + try/finally 清理；purge 一律带 project_id 调用，
绝不跑无 scope 的生产默认路径（那会删掉开发机上真实项目的过期行）。
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig
from swarm.memory.decay import MemoryDecay
from swarm.memory.store import MemoryStore

pytestmark = pytest.mark.needs_service("pg")


def _conn():
    return psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True)


@pytest.fixture()
def pid():
    """每个用例独立 project_id，避免 purge 的破坏性互相干扰。"""
    p = f"_test_a5m2_{uuid.uuid4().hex[:8]}"
    try:
        yield p
    finally:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM mem_mistakes WHERE project_id = %s", (p,))
            cur.execute("DELETE FROM mem_successes WHERE project_id = %s", (p,))


# 四类夹具行：weight 取值使 effective_weight 明确落在 DECAY_DELETE_THRESHOLD(0.05) 两侧。
# last_seen_at/last_used_at 取默认 now() ⇒ age≈0 ⇒ effective≈base，判定只由 base 决定。
_SEED_MISTAKES = [
    # (标签, decay_weight, status)
    ("live_high", 1.0, None),      # 活跃高权重：purge 后必须在（对照，证明没把整表删空）
    ("plain_expired", 0.01, None),  # 无标记 + 已过期：purge 后必须没了（★区分力★）
    ("dismissed", 0.0, "dismissed"),  # 人工裁决：base=0 ⇒ 无谓词必被删
    ("merged", 0.01, "merged"),       # 整合碎片：已过期 ⇒ 无谓词必被删
]


def _seed(pid: str) -> None:
    with _conn() as conn, conn.cursor() as cur:
        for label, w, status in _SEED_MISTAKES:
            meta = "{}" if status is None else f'{{"status": "{status}"}}'
            cur.execute(
                "INSERT INTO mem_mistakes "
                "(project_id, error_type, description, decay_weight, metadata_json) "
                "VALUES (%s, 'compile_error', %s, %s, %s::jsonb)",
                (pid, label, w, meta),
            )
            cur.execute(
                "INSERT INTO mem_successes "
                "(project_id, pattern_name, description, decay_weight, metadata_json) "
                "VALUES (%s, %s, 'd', %s, %s::jsonb)",
                (pid, label, w, meta),
            )


def _labels(pid: str) -> tuple[set[str], set[str]]:
    """返回 (mem_mistakes 存活标签集, mem_successes 存活标签集)。"""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT description FROM mem_mistakes WHERE project_id = %s", (pid,))
        m = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT pattern_name FROM mem_successes WHERE project_id = %s", (pid,))
        s = {r[0] for r in cur.fetchall()}
    return m, s


def _purge(pid: str) -> dict:
    async def _run():
        store = MemoryStore()
        await store.connect()
        try:
            return await MemoryDecay(store).purge_expired(project_id=pid)
        finally:
            await store.close()

    return asyncio.run(_run())


def _seed_and_purge(pid: str) -> tuple[set[str], set[str]]:
    _seed(pid)
    before_m, before_s = _labels(pid)
    assert before_m == {lbl for lbl, _, _ in _SEED_MISTAKES}, (
        f"夹具前提不成立：mem_mistakes 未按预期播种，得 {before_m}"
    )
    assert before_s == {lbl for lbl, _, _ in _SEED_MISTAKES}, (
        f"夹具前提不成立：mem_successes 未按预期播种，得 {before_s}"
    )
    _purge(pid)
    return _labels(pid)


# ── 区分力：普通过期行照删（缺这条，"谓词恒假/什么都不删" 也能全绿）──

def test_plain_expired_row_is_still_deleted(pid):
    """无 status 标记的过期行必须照旧被物理删——purge 的本职没被留存谓词打死。"""
    m, s = _seed_and_purge(pid)
    assert "plain_expired" not in m, f"mem_mistakes 过期行应被删，存活={m}"
    assert "plain_expired" not in s, f"mem_successes 过期行应被删，存活={s}"


def test_live_high_weight_row_survives(pid):
    """对照：活跃高权重行不受影响（证明不是把整表删空）。"""
    m, s = _seed_and_purge(pid)
    assert "live_high" in m and "live_high" in s, f"活跃行被误删 m={m} s={s}"


# ── 留存：两张表各一条（治法改一条 DELETE ＝半落地）──

def test_dismissed_mistake_survives_purge(pid):
    """人工 dismiss 的错题（base=0 ⇒ effective 恒 0）必须留存，审计痕迹不可蒸发。"""
    m, _ = _seed_and_purge(pid)
    assert "dismissed" in m, f"dismissed 行被物理删，存活={m}"


def test_merged_mistake_survives_purge(pid):
    """整合标 merged 的错题必须留存，它是 dedup_rate 的分母。"""
    m, _ = _seed_and_purge(pid)
    assert "merged" in m, f"merged 行被物理删，存活={m}"


def test_dismissed_and_merged_success_survive_purge(pid):
    """★mem_successes 那条 DELETE 独立断言★：只改 mem_mistakes 一条 = 半落地，此处必红。"""
    _, s = _seed_and_purge(pid)
    assert {"dismissed", "merged"} <= s, f"mem_successes 的标记行被物理删，存活={s}"


# ── 真实生产写者产出的 dismissed 行（不手工造 status 取值）──

def test_dismiss_mistake_then_purge_keeps_row(pid):
    """端到端：走生产写者 `dismiss_mistake` 置标记，再 purge，行仍在。

    刻意不手工 INSERT status='dismissed'——手造取值会把"生产真会写出这个形态"从命题里抹掉。
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO mem_mistakes (project_id, error_type, description) "
            "VALUES (%s, 'compile_error', 'via_api') RETURNING id",
            (pid,),
        )
        mid = cur.fetchone()[0]

    async def _run():
        store = MemoryStore()
        await store.connect()
        try:
            ok = await store.dismiss_mistake(pid, mid)
            assert ok, "dismiss_mistake 未命中行，夹具前提不成立"
            return await MemoryDecay(store).purge_expired(project_id=pid)
        finally:
            await store.close()

    asyncio.run(_run())

    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT decay_weight, metadata_json->>'status' FROM mem_mistakes WHERE id = %s",
            (mid,),
        )
        row = cur.fetchone()
    assert row is not None, "被 dismiss 的错题在 purge 后消失了（人工裁决痕迹丢失）"
    assert float(row[0]) == 0.0 and row[1] == "dismissed", f"前提校验：{row}"


# ── 指标：purge 不改变 merged 计数 ⇒ dedup_rate 不因清理而漂 ──

def test_purge_does_not_shrink_dedup_denominator(pid):
    """merged 计数在 purge 前后相等 ⇒ dedup_rate 不被后台清理侵蚀。"""
    _seed(pid)

    async def _health():
        store = MemoryStore()
        await store.connect()
        try:
            return await MemoryDecay(store).get_memory_health(pid)
        finally:
            await store.close()

    before = asyncio.run(_health())
    _purge(pid)
    after = asyncio.run(_health())

    for section in ("mistakes", "successes"):
        assert after[section]["merged"] == before[section]["merged"] == 1, (
            f"{section}: merged 计数被 purge 改变 "
            f"{before[section]['merged']} → {after[section]['merged']}"
        )
        assert after[section]["dedup_rate"] > 0.0, (
            f"{section}: dedup_rate 被 purge 打到 0，得 {after[section]}"
        )
