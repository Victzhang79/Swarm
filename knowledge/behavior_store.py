"""Layer D — 历史行为: 共现分析、高频修改，PostgreSQL 存储

负责:
- 文件修改记录(哪些文件被频繁修改)
- 共现分析(哪些文件经常一起修改)
- Hotspot 检测(频繁修改的文件排序)
- 基于 co-occurrence 的相关文件推荐
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import psycopg

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

# P2：单任务参与共现计算的文件数上限。超过即跳过(共现是 O(n²) 对，巨型任务既无意义又
# 拖垮写入)。40 文件 → 780 对，已是上界；更大基本是批量重排/格式化，非"经常一起改"信号。
_CO_OCCURRENCE_MAX_FILES = 40

# ──────────────────────────────────────────────
# PG DDL — 行为存储
# ──────────────────────────────────────────────

MODIFICATION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS kb_modification_log (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    task_id         TEXT,
    file_path       TEXT        NOT NULL,
    change_type     TEXT        DEFAULT 'modify',   -- add / modify / delete
    commit_hash     TEXT,
    author          TEXT,
    modified_at     TIMESTAMPTZ DEFAULT now(),
    metadata_json   JSONB       DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_mod_project    ON kb_modification_log(project_id);
CREATE INDEX IF NOT EXISTS idx_mod_file       ON kb_modification_log(project_id, file_path);
CREATE INDEX IF NOT EXISTS idx_mod_task       ON kb_modification_log(project_id, task_id);
CREATE INDEX IF NOT EXISTS idx_mod_time       ON kb_modification_log(project_id, modified_at DESC);
"""

CO_OCCURRENCE_DDL = """
CREATE TABLE IF NOT EXISTS kb_co_occurrence (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    file_a          TEXT        NOT NULL,
    file_b          TEXT        NOT NULL,
    co_count        INT         DEFAULT 1,
    last_co_seen    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(project_id, file_a, file_b)
);

CREATE INDEX IF NOT EXISTS idx_coo_project ON kb_co_occurrence(project_id);
CREATE INDEX IF NOT EXISTS idx_coo_file_a ON kb_co_occurrence(project_id, file_a);
CREATE INDEX IF NOT EXISTS idx_coo_file_b ON kb_co_occurrence(project_id, file_b);
"""


@dataclass
class ModificationRecord:
    """单条文件修改记录"""
    file_path: str
    task_id: str | None = None
    change_type: str = "modify"       # add / modify / delete
    commit_hash: str | None = None
    author: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BehaviorStore:
    """Layer D — 历史行为存储

    记录文件修改日志，计算共现频率 & 高频修改文件。
    """

    ALL_DDL = [MODIFICATION_LOG_DDL, CO_OCCURRENCE_DDL]

    def __init__(self, db_config: DatabaseConfig | None = None) -> None:
        self._db_config = db_config or DatabaseConfig()
        self._conn: psycopg.AsyncConnection | None = None

    # ── 连接管理 ──────────────────────────────

    async def connect(self) -> None:
        self._conn = await psycopg.AsyncConnection.connect(
            self._db_config.postgres_uri, autocommit=True
        )
        await self.ensure_tables()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def ensure_tables(self) -> None:
        assert self._conn
        async with self._conn.cursor() as cur:
            for ddl in self.ALL_DDL:
                await cur.execute(ddl)
        logger.info("BehaviorStore tables ensured")

    def _conn_or_raise(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("BehaviorStore not connected — call connect() first")
        return self._conn

    # ── 写入: 修改日志 ──────────────────────────

    async def log_modification(
        self, project_id: str, record: ModificationRecord
    ) -> None:
        """记录一条文件修改"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO kb_modification_log
                    (project_id, task_id, file_path, change_type, commit_hash, author, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (project_id, record.task_id, record.file_path, record.change_type,
                 record.commit_hash, record.author,
                 psycopg.types.json.Jsonb(record.metadata)),
            )

    async def log_modifications_batch(
        self, project_id: str, records: list[ModificationRecord]
    ) -> None:
        """批量记录修改 + 自动更新共现"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO kb_modification_log
                    (project_id, task_id, file_path, change_type, commit_hash, author, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (project_id, r.task_id, r.file_path, r.change_type,
                     r.commit_hash, r.author,
                     psycopg.types.json.Jsonb(r.metadata))
                    for r in records
                ],
            )

        # 更新共现关系(同一 task_id 的文件互为共现)
        await self._update_co_occurrences(project_id, records)

    # ── 共现分析 ────────────────────────────────

    async def _update_co_occurrences(
        self, project_id: str, records: list[ModificationRecord]
    ) -> None:
        """根据同一 task_id 的修改更新共现计数"""
        # 按 task_id 分组
        by_task: dict[str, list[str]] = defaultdict(list)
        for r in records:
            if r.task_id:
                by_task[r.task_id].append(r.file_path)

        # P2：① 单任务文件数上限——共现是 O(n²) 对，巨型任务(一次改几十上百文件)既产生
        # 海量噪声对又拖垮写入。超 _CO_OCCURRENCE_MAX_FILES 的任务跳过共现(那已非有意义的
        # "经常一起改"信号)。② 所有对【批量 executemany】单次往返，替代逐对 await。
        params: list[tuple[str, str, str]] = []
        for _task_id, files in by_task.items():
            unique_files = sorted(set(files))
            if len(unique_files) > _CO_OCCURRENCE_MAX_FILES:
                logger.info(
                    "[co-occurrence] task 改动 %d 文件超上限 %d，跳过共现(避免 O(n²) 噪声膨胀)",
                    len(unique_files), _CO_OCCURRENCE_MAX_FILES,
                )
                continue
            for i in range(len(unique_files)):
                for j in range(i + 1, len(unique_files)):
                    params.append((project_id, unique_files[i], unique_files[j]))
        if not params:
            return
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO kb_co_occurrence (project_id, file_a, file_b, co_count, last_co_seen)
                VALUES (%s, %s, %s, 1, now())
                ON CONFLICT (project_id, file_a, file_b) DO UPDATE SET
                    co_count    = kb_co_occurrence.co_count + 1,
                    last_co_seen = now()
                """,
                params,
            )

    # ── 查询: 高频修改文件 ──────────────────────

    async def get_hotspot_files(
        self, project_id: str, top_k: int = 20, days: int | None = None
    ) -> list[dict[str, Any]]:
        """高频修改文件排行(hotspot 检测)

        Args:
            top_k: 返回 top K
            days: 仅统计最近 N 天(可选)
        """
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            if days:
                await cur.execute(
                    """
                    SELECT file_path, COUNT(*) AS mod_count, MAX(modified_at) AS last_modified
                    FROM kb_modification_log
                    WHERE project_id = %s AND modified_at >= now() - make_interval(days => %s)
                    GROUP BY file_path
                    ORDER BY mod_count DESC
                    LIMIT %s
                    """,
                    (project_id, days, top_k),
                )
            else:
                await cur.execute(
                    """
                    SELECT file_path, COUNT(*) AS mod_count, MAX(modified_at) AS last_modified
                    FROM kb_modification_log
                    WHERE project_id = %s
                    GROUP BY file_path
                    ORDER BY mod_count DESC
                    LIMIT %s
                    """,
                    (project_id, top_k),
                )
            rows = await cur.fetchall()
        return [
            {"file_path": r[0], "mod_count": r[1], "last_modified": r[2]}
            for r in rows
        ]

    # ── 查询: 共现文件 ──────────────────────────

    async def get_co_occurring_files(
        self, project_id: str, file_path: str, top_k: int = 10
    ) -> list[dict[str, Any]]:
        """给定一个文件，返回与之经常一起修改的文件(共现分析)"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    CASE WHEN file_a = %s THEN file_b ELSE file_a END AS co_file,
                    co_count,
                    last_co_seen
                FROM kb_co_occurrence
                WHERE project_id = %s AND (file_a = %s OR file_b = %s)
                ORDER BY co_count DESC
                LIMIT %s
                """,
                (file_path, project_id, file_path, file_path, top_k),
            )
            rows = await cur.fetchall()
        return [
            {"file_path": r[0], "co_count": r[1], "last_co_seen": r[2]}
            for r in rows
        ]

    # ── 查询: 修改历史 ──────────────────────────

    async def get_file_history(
        self, project_id: str, file_path: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """查询单文件的修改历史"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT task_id, change_type, commit_hash, author, modified_at, metadata_json
                FROM kb_modification_log
                WHERE project_id = %s AND file_path = %s
                ORDER BY modified_at DESC
                LIMIT %s
                """,
                (project_id, file_path, limit),
            )
            rows = await cur.fetchall()
        return [
            {
                "task_id": r[0],
                "change_type": r[1],
                "commit_hash": r[2],
                "author": r[3],
                "modified_at": r[4],
                "metadata": r[5],
            }
            for r in rows
        ]

    # ── 清理 ────────────────────────────────────

    async def prune_old_logs(self, project_id: str, retention_days: int = 180) -> int:
        """清理过旧的修改日志（顺带清理陈旧共现条目，防 kb_co_occurrence 无界）。"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM kb_modification_log
                WHERE project_id = %s AND modified_at < now() - make_interval(days => %s)
                """,
                (project_id, retention_days),
            )
            deleted = cur.rowcount
            # P2：共现表同样按 last_co_seen 过期清理，避免只增不删无界膨胀。
            await cur.execute(
                """
                DELETE FROM kb_co_occurrence
                WHERE project_id = %s AND last_co_seen < now() - make_interval(days => %s)
                """,
                (project_id, retention_days),
            )
            co_deleted = cur.rowcount
        logger.info(
            "Pruned %d old modification logs + %d stale co-occurrences for project %s",
            deleted, co_deleted, project_id,
        )
        return deleted
