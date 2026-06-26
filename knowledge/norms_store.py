"""Layer C — 项目规范: PostgreSQL 存储，按 project_id 全量读取

负责:
- 项目规范(编码规范、架构约定、命名风格等)的 CRUD
- 按 project_id 全量读取供 Brain 使用
- 规范可带 tag 分类(如 "naming", "architecture", "testing")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# PG DDL — 项目规范表
# ──────────────────────────────────────────────

NORMS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS kb_norms (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    title           TEXT        NOT NULL,
    content         TEXT        NOT NULL,
    tag             TEXT        DEFAULT 'general',   -- naming / architecture / testing / general
    priority        INT         DEFAULT 0,           -- 越高越优先
    is_active       BOOLEAN     DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    metadata_json   JSONB       DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_norms_project   ON kb_norms(project_id);
CREATE INDEX IF NOT EXISTS idx_norms_tag       ON kb_norms(project_id, tag);
CREATE INDEX IF NOT EXISTS idx_norms_active    ON kb_norms(project_id, is_active);
"""


@dataclass
class Norm:
    """单条项目规范"""
    title: str
    content: str
    tag: str = "general"
    priority: int = 0
    is_active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class NormsStore:
    """Layer C — 项目规范存储

    规范按 project_id 组织，通常量不大，Brain 使用时全量读取。
    """

    ALL_DDL = [NORMS_TABLE_DDL]

    def __init__(self, db_config: DatabaseConfig | None = None) -> None:
        self._db_config = db_config or DatabaseConfig()
        self._conn: psycopg.AsyncConnection | None = None

    # ── 连接管理 ──────────────────────────────

    async def connect(self) -> None:
        if self._conn is not None:
            return  # TD2606-B16：幂等守卫——重复 connect 不再丢弃旧连接造成泄漏
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
        logger.info("NormsStore tables ensured")

    def _conn_or_raise(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("NormsStore not connected — call connect() first")
        return self._conn

    # ── 写入 ────────────────────────────────────

    async def add_norm(self, project_id: str, norm: Norm) -> int:
        """添加一条规范，返回 id"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO kb_norms (project_id, title, content, tag, priority, is_active, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (project_id, norm.title, norm.content, norm.tag, norm.priority,
                 norm.is_active, psycopg.types.json.Jsonb(norm.metadata)),
            )
            row = await cur.fetchone()
        return row[0]

    async def add_norms_batch(self, project_id: str, norms: list[Norm]) -> list[int]:
        """批量添加规范，返回 id 列表"""
        conn = self._conn_or_raise()
        ids = []
        async with conn.cursor() as cur:
            for norm in norms:
                await cur.execute(
                    """
                    INSERT INTO kb_norms (project_id, title, content, tag, priority, is_active, metadata_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (project_id, norm.title, norm.content, norm.tag, norm.priority,
                     norm.is_active, psycopg.types.json.Jsonb(norm.metadata)),
                )
                row = await cur.fetchone()
                ids.append(row[0])
        return ids

    # ── 更新 ────────────────────────────────────

    async def update_norm(
        self, project_id: str, norm_id: int, **fields
    ) -> None:
        """更新指定规范的字段"""
        conn = self._conn_or_raise()
        allowed = {"title", "content", "tag", "priority", "is_active", "metadata_json"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [project_id, norm_id]

        # metadata_json 特殊处理
        if "metadata_json" in updates:
            idx = list(updates.keys()).index("metadata_json")
            values[idx] = psycopg.types.json.Jsonb(updates["metadata_json"])

        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE kb_norms SET {set_clause}, updated_at = now()
                WHERE project_id = %s AND id = %s
                """,
                values,
            )

    # ── 查询: 全量读取 ─────────────────────────

    async def get_all_norms(
        self, project_id: str, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """按 project_id 全量读取规范(Brain 使用)"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            if active_only:
                await cur.execute(
                    """
                    SELECT id, title, content, tag, priority, is_active, metadata_json
                    FROM kb_norms
                    WHERE project_id = %s AND is_active = TRUE
                    ORDER BY priority DESC, id ASC
                    """,
                    (project_id,),
                )
            else:
                await cur.execute(
                    """
                    SELECT id, title, content, tag, priority, is_active, metadata_json
                    FROM kb_norms
                    WHERE project_id = %s
                    ORDER BY priority DESC, id ASC
                    """,
                    (project_id,),
                )
            rows = await cur.fetchall()

        return [
            {
                "id": r[0],
                "title": r[1],
                "content": r[2],
                "tag": r[3],
                "priority": r[4],
                "is_active": r[5],
                "metadata": r[6],
            }
            for r in rows
        ]

    # ── 查询: 按 tag ────────────────────────────

    async def get_norms_by_tag(
        self, project_id: str, tag: str
    ) -> list[dict[str, Any]]:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, title, content, tag, priority, is_active, metadata_json
                FROM kb_norms
                WHERE project_id = %s AND tag = %s AND is_active = TRUE
                ORDER BY priority DESC, id ASC
                """,
                (project_id, tag),
            )
            rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "content": r[2],
                "tag": r[3],
                "priority": r[4],
                "is_active": r[5],
                "metadata": r[6],
            }
            for r in rows
        ]

    # ── 删除 ────────────────────────────────────

    async def delete_norm(self, project_id: str, norm_id: int) -> None:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM kb_norms WHERE project_id = %s AND id = %s",
                (project_id, norm_id),
            )

    async def delete_all_norms(self, project_id: str) -> int:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM kb_norms WHERE project_id = %s",
                (project_id,),
            )
            return cur.rowcount

    async def delete_norms_by_tag(self, project_id: str, tag: str) -> int:
        """按 project_id + tag 删除规范，返回删除行数（供重提取时清理旧 auto 规范）"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM kb_norms WHERE project_id = %s AND tag = %s",
                (project_id, tag),
            )
            return cur.rowcount
