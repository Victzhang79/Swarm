"""L5/L6 批量碎片整合 — 把写时去重漏网的近义条目机械合并(WS3)。

为什么需要：写时去重(`brain/learn_store.py` cosine≥0.92, top_k=1)在 embedding 服务挂时
退化为零向量→查不到近邻→直接插新，于是同一类错误/同一成功模式会在库里沉积多条近义碎片。
本 job 周期性地按 project 全量自连接(pgvector)找 cos≥θ 的簇，机械合并：
  - 选代表(representative)：occurrence/reuse 最高者(并列取 last_seen 最新、再并列取最小 id)。
  - 代表吸收：count 累加、时间戳取最新、base 锚点权重取最大(保留最强证据)。
  - 其余标 metadata.status='merged' + merged_into=rep，检索时被 NOT IN ('merged') 过滤掉。
合并是幂等的(已 merged 的不再参与)，可安全重复运行。dedup_rate 指标据此验收。
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from swarm.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# 整合相似度阈值：略高于写时去重的 0.92，保守合并(只并“几乎肯定重复”的)，避免误并近邻但不同的条目。
DEFAULT_CONSOLIDATE_THRESHOLD = 0.93


def cluster_pairs(pairs: list[tuple[Any, Any]], nodes: set) -> list[list]:
    """并查集：把成对的近义关系聚成簇。返回 size>1 的簇(纯函数，可独立单测)。"""
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # 路径压缩
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    clusters: dict[Any, list] = {}
    for n in nodes:
        clusters.setdefault(find(n), []).append(n)
    return [sorted(c) for c in clusters.values() if len(c) > 1]


def pick_representative(rows: dict[Any, dict]) -> Any:
    """选簇代表：count 最高 → last 时间最新 → id 最小。rows: id -> {count, last, ...}。"""
    return max(
        rows,
        key=lambda i: (rows[i]["count"], rows[i]["last"] or 0, -_as_int(i)),
    )


def _as_int(x: Any) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


class MemoryConsolidator:
    """L5/L6 批量碎片整合器。"""

    # (表名, 类型列(同类才并，None=不限), 计数列, 时间列) —— 表名/列名均为硬编码常量，非用户输入。
    _L5 = ("mem_mistakes", "error_type", "occurrence_count", "last_seen_at")
    _L6 = ("mem_successes", None, "reuse_count", "last_used_at")

    def __init__(
        self, store: MemoryStore, threshold: float = DEFAULT_CONSOLIDATE_THRESHOLD
    ) -> None:
        self._store = store
        self.threshold = threshold

    async def consolidate_mistakes(self, project_id: str) -> dict[str, Any]:
        return await self._consolidate(project_id, *self._L5)

    async def consolidate_successes(self, project_id: str) -> dict[str, Any]:
        return await self._consolidate(project_id, *self._L6)

    async def consolidate_all(self, project_id: str) -> dict[str, Any]:
        l5 = await self.consolidate_mistakes(project_id)
        l6 = await self.consolidate_successes(project_id)
        return {"l5": l5, "l6": l6}

    async def discover_projects(self) -> list[str]:
        """枚举库内所有有 L5/L6 记录的 project_id（供后台 job 全量整合）。"""
        conn = self._store._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT project_id FROM mem_mistakes "
                "UNION SELECT project_id FROM mem_successes"
            )
            return [r[0] for r in await cur.fetchall()]

    async def consolidate_projects(
        self, project_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """整合给定项目；project_ids=None 时自动枚举全库项目。返回逐项目统计。"""
        if project_ids is None:
            project_ids = await self.discover_projects()
        out: dict[str, Any] = {}
        for pid in project_ids:
            try:
                out[pid] = await self.consolidate_all(pid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("consolidate project=%s 失败: %s", pid, exc)
                out[pid] = {"error": str(exc)}
        return out

    # ── 内部 ────────────────────────────────────

    async def _dup_pairs(
        self, project_id: str, table: str, type_col: str | None
    ) -> list[tuple[int, int]]:
        """自连接找 cos≥θ 的活跃近义对(跳过零向量占位与已 merged/archived/dismissed)。"""
        conn = self._store._conn_or_raise()
        type_join = f"AND a.{type_col} = b.{type_col}" if type_col else ""
        sql = f"""
            SELECT a.id, b.id
            FROM {table} a
            JOIN {table} b
              ON a.project_id = b.project_id
             AND a.id < b.id
             {type_join}
             AND (1 - (a.embedding <=> b.embedding)) >= %s
            WHERE a.project_id = %s
              AND COALESCE(a.metadata_json->>'status', '') NOT IN ('archived', 'dismissed', 'merged')
              AND COALESCE(b.metadata_json->>'status', '') NOT IN ('archived', 'dismissed', 'merged')
              AND COALESCE((a.metadata_json->>'embedding_placeholder')::bool, false) = false
              AND COALESCE((b.metadata_json->>'embedding_placeholder')::bool, false) = false
        """
        async with conn.cursor() as cur:
            await cur.execute(sql, (self.threshold, project_id))
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def _fetch_rows(
        self, table: str, count_col: str, time_col: str, ids: list[int]
    ) -> dict[int, dict]:
        conn = self._store._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT id, {count_col}, {time_col}, decay_weight "
                f"FROM {table} WHERE id = ANY(%s)",
                (ids,),
            )
            rows = await cur.fetchall()
        return {
            r[0]: {"count": r[1] or 0, "last": r[2], "weight": float(r[3] or 0.0)}
            for r in rows
        }

    async def _consolidate(
        self, project_id: str, table: str, type_col: str | None,
        count_col: str, time_col: str,
    ) -> dict[str, Any]:
        pairs = await self._dup_pairs(project_id, table, type_col)
        nodes: set[int] = set()
        for a, b in pairs:
            nodes.update((a, b))
        clusters = cluster_pairs(pairs, nodes)

        merged = 0
        conn = self._store._conn_or_raise()
        async with self._store.transaction():
            for cluster in clusters:
                rows = await self._fetch_rows(table, count_col, time_col, cluster)
                if not rows:
                    continue
                rep = pick_representative(rows)
                others = [i for i in cluster if i != rep]
                if not others:
                    continue
                total = sum(rows[i]["count"] for i in cluster)
                last = max((rows[i]["last"] for i in cluster if rows[i]["last"]), default=None)
                weight = max(rows[i]["weight"] for i in cluster)
                async with conn.cursor() as cur:
                    # 代表吸收：累加计数、时间戳取最新、base 取最强
                    await cur.execute(
                        f"UPDATE {table} SET {count_col} = %s, {time_col} = COALESCE(%s, {time_col}), "
                        f"decay_weight = %s WHERE id = %s",
                        (total, last, weight, rep),
                    )
                    # 其余标 merged + 回指代表
                    await cur.execute(
                        f"UPDATE {table} SET metadata_json = "
                        f"COALESCE(metadata_json, '{{}}'::jsonb) || %s "
                        f"WHERE id = ANY(%s)",
                        (psycopg.types.json.Jsonb({"status": "merged", "merged_into": rep}), others),
                    )
                merged += len(others)

        stats = {"pairs": len(pairs), "clusters": len(clusters), "merged": merged}
        logger.info(
            "consolidate(%s): pairs=%d clusters=%d merged=%d",
            table, stats["pairs"], stats["clusters"], stats["merged"],
        )
        return stats
