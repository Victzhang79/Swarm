"""L5 错题集 / L6 成功模式 衰减机制

★DR-08-F6(#84) 更正★：WS1 起衰减已迁到【query 读时现算】——effective_weight 在读取时按
last_seen_at 年龄计算（见 store._effective_weight_sql_l5/l6），base 列 decay_weight 不再被后台
乘减。`decay_l5_batch_sql`/`decay_l6_batch_sql` 是【已废弃的死路径】，误调会与读时衰减叠加成双重
衰减（错题过早被 purge_expired 删）→ 已改为 raise NotImplementedError。后台维护【唯一入口=
purge_expired】（start_daily_decay 也只调 purge_expired）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from swarm.memory.store import (
    DECAY_DELETE_THRESHOLD,
    L5_DECAY_FACTOR,
    L6_DECAY_FACTOR,
    MemoryStore,
    _effective_weight_sql_l5,
    _effective_weight_sql_l6,
)

logger = logging.getLogger(__name__)


class MemoryDecay:
    """L5/L6 衰减管理器

    使用方式（★衰减读时现算，后台只 purge★）:
        decay = MemoryDecay(memory_store)
        await decay.connect()
        await decay.start_daily_decay()          # 唯一后台维护：周期 purge_expired
        # decay_l5_batch_sql/decay_l6_batch_sql 已废弃（#84）：会双重衰减 → raise
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        decay_factor: float = L5_DECAY_FACTOR,
        l6_decay_factor: float = L6_DECAY_FACTOR,
        delete_threshold: float = DECAY_DELETE_THRESHOLD,
        occurrence_boost: bool = True,
    ) -> None:
        """初始化衰减参数

        Args:
            memory_store: MemoryStore 实例(需已连接)
            decay_factor: L5 每日衰减因子(0.9 = 每天衰减 10%)
            l6_decay_factor: L6 每日衰减因子(0.95 = 每天衰减 5%，比 L5 更温和)
            delete_threshold: 低于此权重则删除
            occurrence_boost: 是否让多次出现的错题衰减更慢
        """
        self._store = memory_store
        self.decay_factor = decay_factor
        self.l6_decay_factor = l6_decay_factor
        self.delete_threshold = delete_threshold
        self.occurrence_boost = occurrence_boost

        # 衰减统计
        self._last_decay_at: datetime | None = None
        self._total_decayed = 0
        self._total_deleted = 0

    # ── 连接代理 ──────────────────────────────

    async def connect(self) -> None:
        """确保底层 MemoryStore 已连接"""
        try:
            self._store._conn_or_raise()
        except RuntimeError:
            await self._store.connect()

    async def close(self) -> None:
        await self._store.close()

    # ── L5 错题集衰减 ──────────────────────────

    async def decay_l5_batch_sql(self) -> dict[str, Any]:
        """★DR-08-F6(#84) 已废弃·禁止调用★：WS1 起衰减迁到【query 读时现算】(effective_weight
        按 last_seen_at 年龄)，base(decay_weight)不再后台乘减。本方法的 `decay_weight *= factor`
        UPDATE 会与读时衰减【叠加=双重衰减】，使错题过早跌破阈值被 purge_expired 提前删除。
        后台维护唯一入口=purge_expired。误调即 fail-loud，绝不静默双衰减。"""
        raise NotImplementedError(
            "decay_l5_batch_sql 已废弃（WS1 read-time 衰减）——base 乘减会与读时 effective_weight "
            "叠加成双重衰减。后台维护请用 purge_expired。")
        conn = self._store._conn_or_raise()
        stats: dict[str, Any] = {
            "total_processed": 0,
            "total_updated": 0,
            "total_deleted": 0,
            "errors": [],
        }

        async with conn.cursor() as cur:
            # 批量衰减：与逐条 decay_l5 公式保持一致——
            # occurrence_boost 开启且 occurrence_count>1 时，多次出现的错题衰减更慢
            # (decay_factor ^ (1/occurrence_count))，否则用平坦 decay_factor。
            if self.occurrence_boost:
                await cur.execute(
                    """
                    UPDATE mem_mistakes
                    SET decay_weight = decay_weight * CASE
                        WHEN COALESCE(occurrence_count, 1) > 1
                            THEN POWER(%s, 1.0 / COALESCE(occurrence_count, 1))
                        ELSE %s
                    END
                    WHERE decay_weight > 0
                    """,
                    (self.decay_factor, self.decay_factor),
                )
            else:
                await cur.execute(
                    """
                    UPDATE mem_mistakes
                    SET decay_weight = decay_weight * %s
                    WHERE decay_weight > 0
                    """,
                    (self.decay_factor,),
                )
            stats["total_updated"] = cur.rowcount
            stats["total_processed"] = cur.rowcount

        async with conn.cursor() as cur:
            # 删除过期
            await cur.execute(
                "DELETE FROM mem_mistakes WHERE decay_weight < %s",
                (self.delete_threshold,),
            )
            stats["total_deleted"] = cur.rowcount

        self._last_decay_at = datetime.now()
        self._total_decayed += stats["total_updated"]
        self._total_deleted += stats["total_deleted"]
        logger.info("decay_l5_batch_sql: updated=%d deleted=%d", stats["total_updated"], stats["total_deleted"])
        return stats

    # ── L6 成功模式衰减 ──────────────────────────

    async def decay_l6_batch_sql(self) -> dict[str, Any]:
        """使用 SQL 批量衰减 L6 成功模式(高效，直接在 DB 层执行)

        ★DR-08-F6(#84) 已废弃·禁止调用★：同 decay_l5_batch_sql——WS1 read-time 衰减后 base 乘减=
        双重衰减，后台维护唯一入口 purge_expired。误调 fail-loud。
        """
        raise NotImplementedError(
            "decay_l6_batch_sql 已废弃（WS1 read-time 衰减）——base 乘减会与读时 effective_weight "
            "叠加成双重衰减。后台维护请用 purge_expired。")
        conn = self._store._conn_or_raise()
        stats: dict[str, Any] = {
            "total_processed": 0,
            "total_updated": 0,
            "total_deleted": 0,
            "errors": [],
        }

        async with conn.cursor() as cur:
            # 批量衰减: reuse_count 高的衰减更慢
            await cur.execute(
                """
                UPDATE mem_successes
                SET decay_weight = decay_weight * POWER(%s, 1.0 / (reuse_count + 1))
                WHERE decay_weight > 0
                """,
                (self.l6_decay_factor,),
            )
            stats["total_updated"] = cur.rowcount
            stats["total_processed"] = cur.rowcount

        async with conn.cursor() as cur:
            # 删除过期
            await cur.execute(
                "DELETE FROM mem_successes WHERE decay_weight < %s",
                (self.delete_threshold,),
            )
            stats["total_deleted"] = cur.rowcount

        self._last_decay_at = datetime.now()
        self._total_decayed += stats["total_updated"]
        self._total_deleted += stats["total_deleted"]
        logger.info("decay_l6_batch_sql: updated=%d deleted=%d", stats["total_updated"], stats["total_deleted"])
        return stats

    # ── WS1 惰性衰减下的物理清理(只删，不乘减) ──

    async def purge_expired(
        self, project_id: str | None = None, as_of: Any = None
    ) -> dict[str, Any]:
        """删除【有效权重】已沉到阈值下的 L5/L6 记录。

        WS1 把“时间衰减”移到了 query 读时现算(effective_weight)，base(decay_weight) 不再被
        后台乘减——否则与读时现算叠加成双重衰减。本方法是退化后的后台 job：仅按 effective_weight
        物理清理过期条目，回收存储；不修改存活条目的 base。as_of 默认 now()，供测试时间旅行。
        """
        conn = self._store._conn_or_raise()
        eff_l5 = _effective_weight_sql_l5()
        eff_l6 = _effective_weight_sql_l6()
        stats: dict[str, Any] = {"l5_deleted": 0, "l6_deleted": 0}

        proj_l5 = ""
        proj_l6 = ""
        l5_params: list[Any] = [self.decay_factor, as_of, self.delete_threshold]
        l6_params: list[Any] = [self.l6_decay_factor, as_of, self.delete_threshold]
        if project_id is not None:
            proj_l5 = proj_l6 = "AND project_id = %s"
            l5_params.append(project_id)
            l6_params.append(project_id)

        async with conn.cursor() as cur:
            await cur.execute(
                f"DELETE FROM mem_mistakes WHERE {eff_l5} < %s {proj_l5}", l5_params
            )
            stats["l5_deleted"] = cur.rowcount
        async with conn.cursor() as cur:
            await cur.execute(
                f"DELETE FROM mem_successes WHERE {eff_l6} < %s {proj_l6}", l6_params
            )
            stats["l6_deleted"] = cur.rowcount

        self._last_decay_at = datetime.now()
        self._total_deleted += stats["l5_deleted"] + stats["l6_deleted"]
        logger.info(
            "purge_expired: l5_deleted=%d l6_deleted=%d", stats["l5_deleted"], stats["l6_deleted"]
        )
        return stats

    # ── 每日自动维护 ────────────────────────────

    def stop_daily_decay(self) -> None:
        """停止后台维护循环（TD2606-C14：原 while True 无停止钩子）。幂等，可在 api 关闭钩子调用。"""
        stop = getattr(self, "_decay_stop", None)
        if stop is not None:
            stop.set()

    async def start_daily_decay(
        self,
        hour: int = 3,
        minute: int = 0,
        project_ids: list[str] | None = None,
        consolidate: bool = True,
    ) -> None:
        """启动每日定时维护(简易实现)

        精确调度应使用外部 cron / APScheduler；此方法为简便的后台循环实现。
        WS1 后：衰减已移至 query 读时现算(effective_weight)，后台 job 退化为
        **只删**过期条目(purge_expired)，不再乘减 base——避免与读时衰减叠加成双重衰减。
        WS3：每轮维护顺带跑一次批量碎片整合(consolidate)，把写时去重漏网的近义碎片合并。

        Args:
            hour: 每日执行时间(时，0-23)
            minute: 每日执行时间(分，0-59)
            project_ids: 需要维护的项目列表(None=全量；整合时自动枚举全库项目)
            consolidate: 是否在每轮维护顺带跑批量碎片整合(默认开)
        """
        from swarm.memory.consolidate import MemoryConsolidator
        consolidator = MemoryConsolidator(self._store)
        import asyncio

        logger.info("Starting daily decay scheduler at %02d:%02d", hour, minute)

        # TD2606-C14：可停止的后台循环。原 `while True` 无停止钩子 → 进程生命周期泄漏
        # task + PG 连接（api 关闭时无法优雅停）。stop_daily_decay() 置位即提前唤醒退出。
        self._decay_stop = asyncio.Event()
        while not self._decay_stop.is_set():
            now = datetime.now()
            # 计算下次执行时间
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                # 今天的时间已过，设为明天
                next_run += timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()

            logger.info("Next decay run at %s (waiting %.0f seconds)", next_run, wait_seconds)
            # 可中断 sleep：被 stop 唤醒 → 退出；正常到点(TimeoutError) → 继续维护。
            try:
                await asyncio.wait_for(self._decay_stop.wait(), timeout=wait_seconds)
                break
            except asyncio.TimeoutError:
                pass

            # 执行清理: 只删过期(L5+L6)，衰减由 query 读时现算
            try:
                if project_ids:
                    for pid in project_ids:
                        await self.purge_expired(project_id=pid)
                else:
                    await self.purge_expired()
                logger.info("Daily purge (L5+L6 expired) completed successfully")
            except Exception as e:
                logger.exception("Daily purge failed: %s", e)

            # 批量碎片整合(WS3): 合并写时去重漏网的近义碎片
            if consolidate:
                try:
                    res = await consolidator.consolidate_projects(project_ids)
                    logger.info("Daily consolidate completed: %d project(s)", len(res))
                except Exception as e:
                    logger.exception("Daily consolidate failed: %s", e)

    # ── 状态查询 ────────────────────────────────

    @property
    def last_decay_at(self) -> datetime | None:
        return self._last_decay_at

    @property
    def total_decayed(self) -> int:
        return self._total_decayed

    @property
    def total_deleted(self) -> int:
        return self._total_deleted

    async def get_decay_stats(self, project_id: str) -> dict[str, Any]:
        """获取衰减统计信息"""
        mistakes = await self._store.get_all_mistakes(project_id, min_weight=0.0)

        if not mistakes:
            return {
                "total_mistakes": 0,
                "avg_weight": 0.0,
                "high_weight_count": 0,    # weight > 0.8
                "medium_weight_count": 0,  # 0.3 < weight <= 0.8
                "low_weight_count": 0,     # weight <= 0.3
            }

        weights = [m["decay_weight"] for m in mistakes]
        high = sum(1 for w in weights if w > 0.8)
        medium = sum(1 for w in weights if 0.3 < w <= 0.8)
        low = sum(1 for w in weights if w <= 0.3)

        return {
            "total_mistakes": len(mistakes),
            "avg_weight": sum(weights) / len(weights),
            "max_weight": max(weights),
            "min_weight": min(weights),
            "high_weight_count": high,
            "medium_weight_count": medium,
            "low_weight_count": low,
            "last_decay_at": self._last_decay_at.isoformat() if self._last_decay_at else None,
        }

    async def get_memory_health(
        self, project_id: str, as_of: Any = None
    ) -> dict[str, Any]:
        """WS4 可观测：L5/L6 记忆规模 + 有效权重分布(读时现算) + 去重(merged)情况。

        权重分布按 effective_weight(惰性时间感知)分桶，反映“当下”而非锚点的记忆健康度；
        dedup_rate = merged / 已存储(含 merged)，量化碎片整合(WS3)清理掉的比例。供 API/巡检消费。
        """
        mistakes = await self._store.get_all_mistakes(project_id, 0.0, as_of=as_of)
        successes = await self._store.get_all_successes(project_id, 0.0, as_of=as_of)
        conn = self._store._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT "
                "(SELECT count(*) FROM mem_mistakes WHERE project_id=%s AND metadata_json->>'status'='merged'),"
                "(SELECT count(*) FROM mem_successes WHERE project_id=%s AND metadata_json->>'status'='merged')",
                (project_id, project_id),
            )
            row = await cur.fetchone()
        m_merged, s_merged = int(row[0]), int(row[1])

        def _summary(rows: list[dict[str, Any]], merged: int) -> dict[str, Any]:
            eff = [float(r.get("effective_weight", 0.0) or 0.0) for r in rows]
            n = len(eff)
            stored = n + merged
            return {
                "stored": stored,                       # 含已 merged
                "active": n,                            # get_all 已按 base>0 粗筛
                "merged": merged,
                "avg_effective_weight": round(sum(eff) / n, 4) if n else 0.0,
                "high_gt_0_8": sum(1 for w in eff if w > 0.8),
                "medium_0_3_0_8": sum(1 for w in eff if 0.3 < w <= 0.8),
                "low_le_0_3": sum(1 for w in eff if w <= 0.3),
                "dedup_rate": round(merged / stored, 4) if stored else 0.0,
            }

        return {
            "project_id": project_id,
            "mistakes": _summary(mistakes, m_merged),
            "successes": _summary(successes, s_merged),
            "last_decay_at": self._last_decay_at.isoformat() if self._last_decay_at else None,
        }

