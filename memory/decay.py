"""L5 错题集 / L6 成功模式 衰减机制 — 指数衰减，每日调用

衰减策略:
- L5 错题集: 每日调用 decay_l5() 执行指数衰减
  - decay_weight *= decay_factor (默认 0.9)
  - 多次出现的错题衰减更慢: effective_factor = decay_factor ^ (1 / occurrence_count)
  - decay_weight 低于 threshold 时删除
  - 重复遇到的错题会重振权重(increment_mistake_occurrence)

- L6 成功模式集: 每日调用 decay_l6() 执行指数衰减(比 L5 更温和)
  - decay_weight *= l6_decay_factor (默认 0.95，比 L5 的 0.9 慢)
  - 高复用次数的衰减更慢: effective_factor = l6_decay_factor ^ (1 / (reuse_count + 1))
  - decay_weight 低于 threshold 时删除
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from swarm.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryDecay:
    """L5/L6 衰减管理器

    使用方式:
        decay = MemoryDecay(memory_store)
        await decay.connect()
        await decay.decay_l5()                   # 执行一次 L5 衰减
        await decay.decay_l6()                   # 执行一次 L6 衰减
        await decay.start_daily_decay()          # 启动每日自动衰减(L5+L6)
    """

    def __init__(
        self,
        memory_store: MemoryStore,
        decay_factor: float = 0.9,
        l6_decay_factor: float = 0.95,
        delete_threshold: float = 0.05,
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

    async def decay_l5(self, project_id: str | None = None) -> dict[str, Any]:
        """对 L5 错题集执行一次指数衰减

        流程:
        1. 获取所有 decay_weight > 0 的错题
        2. 对每条计算新的 decay_weight
        3. 更新数据库
        4. 删除低于阈值的错题

        Args:
            project_id: 指定项目(为 None 则衰减所有项目 — 全表批量)

        Returns:
            统计信息 dict
        """
        stats: dict[str, Any] = {
            "total_processed": 0,
            "total_updated": 0,
            "total_deleted": 0,
            "errors": [],
        }

        if project_id is None:
            # 全项目衰减: 使用批量 SQL(高效，直接全表扫描)
            return await self.decay_l5_batch_sql()

        # 指定项目: 逐条衰减(未来可按需改为 batch)
        mistakes = await self._store.get_all_mistakes(project_id, min_weight=0.0)
        stats["total_processed"] = len(mistakes)

        for m in mistakes:
            try:
                old_weight = m["decay_weight"]
                occurrence_count = m.get("occurrence_count", 1)

                # 计算有效衰减因子
                if self.occurrence_boost and occurrence_count > 1:
                    # 多次出现的错题衰减更慢
                    effective_factor = self.decay_factor ** (1.0 / occurrence_count)
                else:
                    effective_factor = self.decay_factor

                new_weight = old_weight * effective_factor

                if new_weight < self.delete_threshold:
                    await self._store.update_mistake_decay_weight(m["id"], 0.0)
                    stats["total_deleted"] += 1
                else:
                    await self._store.update_mistake_decay_weight(m["id"], new_weight)
                    stats["total_updated"] += 1

            except Exception as e:
                logger.exception("Error decaying mistake %d", m.get("id"))
                stats["errors"].append({"id": m.get("id"), "error": str(e)})

        # 清理极低权重的错题
        try:
            deleted_count = await self._store.delete_expired_mistakes(
                min_weight=self.delete_threshold
            )
            stats["total_deleted"] += deleted_count
        except Exception as e:
            logger.exception("Error cleaning expired mistakes")
            stats["errors"].append({"phase": "cleanup", "error": str(e)})

        self._last_decay_at = datetime.now()
        self._total_decayed += stats["total_updated"]
        self._total_deleted += stats["total_deleted"]

        logger.info(
            "decay_l5: processed=%d updated=%d deleted=%d errors=%d",
            stats["total_processed"],
            stats["total_updated"],
            stats["total_deleted"],
            len(stats["errors"]),
        )

        return stats

    async def decay_l5_batch_sql(self) -> dict[str, Any]:
        """使用 SQL 批量衰减 L5(高效，直接在 DB 层执行)

        对全表执行: UPDATE mem_mistakes SET decay_weight = decay_weight * decay_factor
        WHERE decay_weight > 0; 然后删除低于阈值的记录。
        """
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

    async def decay_l6(self, project_id: str | None = None) -> dict[str, Any]:
        """对 L6 成功模式集执行一次指数衰减

        衰减公式: decay_weight *= l6_decay_factor ^ (1 / (reuse_count + 1))
        即 reuse_count 越高衰减越慢，成功模式比错题衰减更温和。

        Args:
            project_id: 指定项目(为 None 则衰减所有项目 — 全表批量)

        Returns:
            统计信息 dict
        """
        stats: dict[str, Any] = {
            "total_processed": 0,
            "total_updated": 0,
            "total_deleted": 0,
            "errors": [],
        }

        if project_id is None:
            # 全项目衰减: 使用批量 SQL
            return await self.decay_l6_batch_sql()

        # 指定项目: 逐条衰减
        successes = await self._store.get_all_successes(project_id, min_weight=0.0)
        stats["total_processed"] = len(successes)

        for s in successes:
            try:
                old_weight = s["decay_weight"]
                reuse_count = s.get("reuse_count", 0)

                # reuse_count 高的衰减更慢
                effective_factor = self.l6_decay_factor ** (1.0 / (reuse_count + 1))
                new_weight = old_weight * effective_factor

                if new_weight < self.delete_threshold:
                    await self._store.update_success_decay_weight(s["id"], 0.0)
                    stats["total_deleted"] += 1
                else:
                    await self._store.update_success_decay_weight(s["id"], new_weight)
                    stats["total_updated"] += 1

            except Exception as e:
                logger.exception("Error decaying success %d", s.get("id"))
                stats["errors"].append({"id": s.get("id"), "error": str(e)})

        # 清理极低权重的成功模式
        try:
            deleted_count = await self._store.delete_expired_successes(
                min_weight=self.delete_threshold
            )
            stats["total_deleted"] += deleted_count
        except Exception as e:
            logger.exception("Error cleaning expired successes")
            stats["errors"].append({"phase": "cleanup", "error": str(e)})

        self._last_decay_at = datetime.now()
        self._total_decayed += stats["total_updated"]
        self._total_deleted += stats["total_deleted"]

        logger.info(
            "decay_l6: processed=%d updated=%d deleted=%d errors=%d",
            stats["total_processed"],
            stats["total_updated"],
            stats["total_deleted"],
            len(stats["errors"]),
        )

        return stats

    async def decay_l6_batch_sql(self) -> dict[str, Any]:
        """使用 SQL 批量衰减 L6 成功模式(高效，直接在 DB 层执行)

        衰减公式: decay_weight = decay_weight * l6_decay_factor ^ (1 / (reuse_count + 1))
        """
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

    # ── 每日自动衰减 ────────────────────────────

    async def start_daily_decay(
        self,
        hour: int = 3,
        minute: int = 0,
        project_ids: list[str] | None = None,
    ) -> None:
        """启动每日定时衰减(简易实现)

        精确调度应使用外部 cron / APScheduler。
        此方法为简便的后台循环实现。
        同时执行 L5 错题集衰减和 L6 成功模式衰减。

        Args:
            hour: 每日执行时间(时，0-23)
            minute: 每日执行时间(分，0-59)
            project_ids: 需要衰减的项目列表(None=全量)
        """
        import asyncio

        logger.info("Starting daily decay scheduler at %02d:%02d", hour, minute)

        while True:
            now = datetime.now()
            # 计算下次执行时间
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                # 今天的时间已过，设为明天
                next_run += timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()

            logger.info("Next decay run at %s (waiting %.0f seconds)", next_run, wait_seconds)
            await asyncio.sleep(wait_seconds)

            # 执行衰减: L5 + L6
            try:
                if project_ids:
                    for pid in project_ids:
                        await self.decay_l5(project_id=pid)
                        await self.decay_l6(project_id=pid)
                else:
                    await self.decay_l5()
                    await self.decay_l6()
                logger.info("Daily decay (L5+L6) completed successfully")
            except Exception as e:
                logger.exception("Daily decay failed: %s", e)

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

