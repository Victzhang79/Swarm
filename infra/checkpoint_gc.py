"""LangGraph checkpoint 三表 GC（round29 运维项收尾：14.4GB 无 TTL 累积）。

checkpoints / checkpoint_blobs / checkpoint_writes 由 langgraph-checkpoint-postgres 只写不删，
全仓此前无任何清理路径（hunter 遗漏项#1 复核登记）。本模块清三类【永不可 resume】的死数据：

1. **终态任务 + TTL 过期**：status ∈ 终态集 且 task_records.updated_at 早于 TTL 天前。
   终态后 checkpoint 仅剩考古价值；中断挂起态（CONFIRMING/DELIVERING/CLARIFYING/
   DESIGN_REVIEW 等非终态）一律不清——人工闸 Command(resume) 依赖其 checkpoint（P0-A）。
2. **孤儿线程**：checkpoints 里存在、task_records 无对应行。任务提交即建行（create_task），
   无行=永不可恢复（历史 e2e reset / delete_project 级联遗留）。
3. **worker 子图 ns 残留**：checkpoint_ns 形如 'dispatch:…'——遗漏项#1（worker react agent
   误继承父 checkpointer）修复前写入的垃圾；任何任务（含活跃任务）都不会 resume 子图 ns，
   修复后不再新增，此处清存量。

纪律：fail-safe（任何异常 → warning + 返回 error 统计，绝不抛出阻断启动）；删除数量/耗时
全量留痕（降级可观测）；TTL env 可调（SWARM_CHECKPOINT_TTL_DAYS，默认 7；<=0 整体禁用）。
删除释放的页由 PG 复用（磁盘瘦身需 VACUUM FULL，运维自行决定，此处不做）。
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# 复核 MEDIUM：终态集引用单一事实源（task_states），不再第三处硬编码副本
from swarm.task_states import TERMINAL_STATES as _TERMINAL_STATUSES
_CKPT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")  # 先子表后主表


def _ttl_days_from_env() -> int:
    raw = os.environ.get("SWARM_CHECKPOINT_TTL_DAYS", "7") or "7"
    try:
        return int(raw)
    except ValueError:
        logger.error(
            "[checkpoint-gc] SWARM_CHECKPOINT_TTL_DAYS 配置非法(%r)——系统级配置错误请修 env，"
            "本次回退默认 7 天", raw,
        )
        return 7


def sweep_stale_checkpoints(ttl_days: int | None = None,
                            conn_str: str | None = None) -> dict:
    """执行一次 checkpoint GC，返回统计 dict（见模块 docstring 的三类清理）。

    ttl_days=None → 读 env（默认 7）；<=0 → 整体禁用（返回 {"disabled": True}）。
    """
    if ttl_days is None:
        ttl_days = _ttl_days_from_env()
    if ttl_days <= 0:
        return {"disabled": True}

    stats: dict = {"ttl_days": ttl_days, "stale_threads": 0, "orphan_threads": 0}
    t0 = time.monotonic()
    try:
        # 复核整改（hunter#2）：get_config 也在 fail-safe 罩内——否则配置层异常会裸穿
        # run_in_executor 落进 _spawn_bg 的静默吞点，违背本函数自己的 docstring 契约。
        if conn_str is None:
            from swarm.config.settings import get_config
            conn_str = get_config().db.postgres_uri
        import psycopg

        from swarm.infra.db import pg_connect_timeout_kwargs

        # D15：直连补 connect_timeout（不加 autocommit，保持原事务语义）。
        with psycopg.connect(conn_str, **pg_connect_timeout_kwargs()) as conn:
            # 1) 终态 + TTL 过期线程
            # ★复核 CRITICAL 整改★：checkpoint 的 thread 键是 task_records.thread_id
            # （retry_task 会改写为 "{id}-r-xxxx"，runner 以该列起图），不是 id——原 join 用
            # t.id 会把【被重跑过的任务】（含活跃态）误判孤儿删其 checkpoint。COALESCE 兜
            # thread_id 为 NULL 的历史旧行。被重跑任务的【旧 thread】(=id) 不再被任何行匹配
            # → 归孤儿清理=正确（retry 铸新 thread 重置状态，旧 thread 永不 resume）。
            stale = [r[0] for r in conn.execute(
                """SELECT COALESCE(thread_id, id) FROM task_records
                   WHERE status = ANY(%s)
                     AND updated_at < now() - make_interval(days => %s)""",
                (list(_TERMINAL_STATUSES), ttl_days)).fetchall()]
            # 2) 孤儿线程（无任何 task_records 行以【当前 thread 键】认领）。
            # 5.9 猎手 F2（HIGH）：E1 打破了"旧 thread 永不 resume"前提——retry_prev_thread_id
            # 指向的旧 thread 是 run_task 播种（保留已 L1 通过产物）的数据源；重启 GC 若先删，
            # 播种静默归零（E1 在"重启后消费 retry 队列"这个最常见场景失效）。豁免：任一
            # 【非终态】任务的播种指针仍引用的 thread 不算孤儿（指针在 run_task 一次性消费
            # 清空后，下轮 GC 照常回收）。
            orphans = [r[0] for r in conn.execute(
                """SELECT DISTINCT c.thread_id FROM checkpoints c
                   LEFT JOIN task_records t ON COALESCE(t.thread_id, t.id) = c.thread_id
                   LEFT JOIN task_records t2 ON t2.retry_prev_thread_id = c.thread_id
                        AND NOT (t2.status = ANY(%s))
                   WHERE t.id IS NULL AND t2.id IS NULL""",
                (list(_TERMINAL_STATUSES),)).fetchall()]
            doomed = sorted(set(stale) | set(orphans))
            stats["stale_threads"] = len(stale)
            stats["orphan_threads"] = len(orphans)
            for table in _CKPT_TABLES:
                deleted = 0
                if doomed:
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (doomed,))
                    deleted += cur.rowcount or 0
                # 3) 子图 ns 残留（对所有线程，含活跃任务——子图 ns 永不 resume）
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE checkpoint_ns LIKE 'dispatch:%'")
                deleted += cur.rowcount or 0
                stats[table] = deleted
            conn.commit()
        stats["seconds"] = round(time.monotonic() - t0, 2)
        logger.info(
            "[checkpoint-gc] 完成：终态过期线程=%d 孤儿线程=%d，删除行 checkpoints=%d "
            "blobs=%d writes=%d（TTL=%d 天，耗时 %.1fs；磁盘瘦身需另行 VACUUM）",
            stats["stale_threads"], stats["orphan_threads"], stats.get("checkpoints", 0),
            stats.get("checkpoint_blobs", 0), stats.get("checkpoint_writes", 0),
            ttl_days, stats["seconds"],
        )
        return stats
    except Exception as exc:  # noqa: BLE001  fail-safe：GC 失败绝不阻断启动
        logger.warning("[checkpoint-gc] 清理失败（不阻断启动，下次启动重试）: %s", exc)
        stats["error"] = str(exc)[:200]
        return stats
