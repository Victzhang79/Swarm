"""L1/L2/L5/L6 统一存储接口 — PostgreSQL + pgvector

负责:
- L1 用户画像: 用户级 JSON 配置
- L2 任务摘要: 滚动 50 条任务摘要
- L5 错题集: 错误模式 + 向量检索
- L6 成功模式集: 成功经验 + 向量检索

pgvector 用于 L5/L6 的向量相似度检索。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

# bge-m3 向量维度
BGE_M3_DIMENSION = 1024

# ──────────────────────────────────────────────
# PG DDL
# ──────────────────────────────────────────────

# pgvector 扩展
ENABLE_PGVECTOR_DDL = "CREATE EXTENSION IF NOT EXISTS vector;"

# L1: 用户画像
USER_PROFILE_DDL = """
CREATE TABLE IF NOT EXISTS mem_user_profile (
    user_id         TEXT        PRIMARY KEY,
    profile_json    JSONB       DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
"""

# L2: 任务摘要(滚动窗口)
TASK_SUMMARY_DDL = """
CREATE TABLE IF NOT EXISTS mem_task_summary (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    task_id         TEXT        NOT NULL,
    summary         TEXT        NOT NULL,
    outcome         TEXT,                          -- success / failure / partial
    lessons_learned TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    metadata_json   JSONB       DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_task_summary_project ON mem_task_summary(project_id);
CREATE INDEX IF NOT EXISTS idx_task_summary_task    ON mem_task_summary(project_id, task_id);
"""

# L5: 错题集(带向量)
MISTAKES_DDL = f"""
CREATE TABLE IF NOT EXISTS mem_mistakes (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    task_id         TEXT,
    error_type      TEXT        NOT NULL,           -- compile_error / test_failure / logic_error / style_violation
    description     TEXT        NOT NULL,
    context         TEXT,                           -- 出错时的上下文
    fix_description TEXT,                           -- 修复方式
    embedding       vector({BGE_M3_DIMENSION}),
    decay_weight    FLOAT       DEFAULT 1.0,        -- 衰减权重
    occurrence_count INT        DEFAULT 1,           -- 出现次数
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_seen_at    TIMESTAMPTZ DEFAULT now(),
    metadata_json   JSONB       DEFAULT '{{}}'
);

CREATE INDEX IF NOT EXISTS idx_mistakes_project  ON mem_mistakes(project_id);
CREATE INDEX IF NOT EXISTS idx_mistakes_type     ON mem_mistakes(project_id, error_type);
"""

# L6: 成功模式集(带向量)
SUCCESSES_DDL = f"""
CREATE TABLE IF NOT EXISTS mem_successes (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    task_id         TEXT,
    pattern_name    TEXT        NOT NULL,
    description     TEXT        NOT NULL,
    approach        TEXT,                           -- 成功方案
    applicable_when TEXT,                           -- 适用条件
    embedding       vector({BGE_M3_DIMENSION}),
    reuse_count     INT         DEFAULT 0,           -- 被重用次数
    decay_weight    FLOAT       DEFAULT 1.0,         -- 衰减权重
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_used_at    TIMESTAMPTZ DEFAULT now(),
    metadata_json   JSONB       DEFAULT '{{}}'
);

CREATE INDEX IF NOT EXISTS idx_successes_project ON mem_successes(project_id);
CREATE INDEX IF NOT EXISTS idx_successes_name    ON mem_successes(project_id, pattern_name);
"""

# 幂等迁移: 为已有 mem_successes 表添加 decay_weight 列
SUCCESSES_MIGRATION_DDL = """
ALTER TABLE mem_successes ADD COLUMN IF NOT EXISTS decay_weight FLOAT DEFAULT 1.0;
"""

# L2 滚动窗口大小
L2_ROLLING_WINDOW = 50


@dataclass
class MistakeEntry:
    """L5 错题条目"""
    error_type: str                   # compile_error / test_failure / logic_error / style_violation
    description: str
    context: str | None = None
    fix_description: str | None = None
    task_id: str | None = None
    embedding: list[float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuccessEntry:
    """L6 成功模式条目"""
    pattern_name: str
    description: str
    approach: str | None = None
    applicable_when: str | None = None
    task_id: str | None = None
    embedding: list[float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskSummary:
    """L2 任务摘要"""
    task_id: str
    summary: str
    outcome: str | None = None         # success / failure / partial
    lessons_learned: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryStore:
    """L1/L2/L5/L6 统一存储接口

    使用 PostgreSQL + pgvector 实现记忆的读写与检索。
    """

    ALL_DDL = [
        ENABLE_PGVECTOR_DDL,
        USER_PROFILE_DDL,
        TASK_SUMMARY_DDL,
        MISTAKES_DDL,
        SUCCESSES_DDL,
        SUCCESSES_MIGRATION_DDL,
    ]

    def __init__(self, db_config: DatabaseConfig | None = None) -> None:
        self._db_config = db_config or DatabaseConfig()
        self._conn: psycopg.AsyncConnection | None = None
        # 占位 embedding 函数
        self._embed_fn = self._default_embed

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
        logger.info("MemoryStore tables ensured")

    def _conn_or_raise(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("MemoryStore not connected — call connect() first")
        return self._conn

    # ── Embedding 占位 ──────────────────────────

    # 模块级标志: 零向量占位只警告一次，避免刷屏
    _placeholder_warned: bool = False

    @staticmethod
    async def _default_embed(texts: list[str]) -> list[list[float]]:
        """默认 embedding：优先专用 embed 服务，不可用回退零向量(告警)。"""
        from swarm.knowledge.embed_client import embed_texts_async
        vecs = await embed_texts_async(texts)
        if vecs is not None:
            return vecs
        if not MemoryStore._placeholder_warned:
            MemoryStore._placeholder_warned = True
            logger.warning(
                "⚠️  Using PLACEHOLDER zero-vector embedding in MemoryStore — "
                "vector search is DISABLED! 配置 SWARM_KB_EMBED_BASE_URL 指向真 bge-m3 服务。",
                stacklevel=2,
            )
        return [[0.0] * BGE_M3_DIMENSION for _ in texts]

    def set_embed_fn(self, fn) -> None:
        """替换 embedding 函数"""
        self._embed_fn = fn

    # ── L1: 用户画像 ────────────────────────────

    async def get_user_profile(self, user_id: str) -> dict[str, Any]:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT profile_json FROM mem_user_profile WHERE user_id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
        return row[0] if row else {}

    async def set_user_profile(self, user_id: str, profile: dict[str, Any]) -> None:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO mem_user_profile (user_id, profile_json, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET
                    profile_json = EXCLUDED.profile_json,
                    updated_at   = now()
                """,
                (user_id, psycopg.types.json.Jsonb(profile)),
            )

    # ── L2: 任务摘要 ────────────────────────────

    async def write_task_summary(
        self, project_id: str, summary: TaskSummary
    ) -> None:
        """写入任务摘要(自动维护滚动窗口)"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO mem_task_summary
                    (project_id, task_id, summary, outcome, lessons_learned, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (project_id, summary.task_id, summary.summary, summary.outcome,
                 summary.lessons_learned, psycopg.types.json.Jsonb(summary.metadata)),
            )

            # 滚动窗口: 保留最近 L2_ROLLING_WINDOW 条
            await cur.execute(
                """
                DELETE FROM mem_task_summary
                WHERE project_id = %s AND id NOT IN (
                    SELECT id FROM mem_task_summary
                    WHERE project_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                )
                """,
                (project_id, project_id, L2_ROLLING_WINDOW),
            )

    async def query_task_summaries(
        self, project_id: str, limit: int = L2_ROLLING_WINDOW
    ) -> list[dict[str, Any]]:
        """查询最近的任务摘要"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT task_id, summary, outcome, lessons_learned, created_at, metadata_json
                FROM mem_task_summary
                WHERE project_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (project_id, limit),
            )
            rows = await cur.fetchall()
        return [
            {
                "task_id": r[0],
                "summary": r[1],
                "outcome": r[2],
                "lessons_learned": r[3],
                "created_at": r[4],
                "metadata": r[5],
            }
            for r in rows
        ]

    # ── L5: 错题集 ──────────────────────────────

    async def write_mistake(
        self, project_id: str, entry: MistakeEntry
    ) -> int:
        """写入一条错题记录(使用 pgvector 格式写入 embedding)"""
        conn = self._conn_or_raise()

        # 生成 embedding
        embed_text = f"{entry.error_type}: {entry.description}"
        if entry.context:
            embed_text += f" | {entry.context}"
        vectors = await self._embed_fn([embed_text])
        embedding = entry.embedding or vectors[0]

        # 检测零向量占位 → 在 metadata 中打标记便于排查
        metadata = dict(entry.metadata) if entry.metadata else {}
        if _is_zero_vector(embedding):
            metadata["embedding_placeholder"] = True

        vector_str = _vector_to_pg(embedding)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO mem_mistakes
                    (project_id, task_id, error_type, description, context,
                     fix_description, embedding, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
                RETURNING id
                """,
                (project_id, entry.task_id, entry.error_type, entry.description,
                 entry.context, entry.fix_description,
                 vector_str,
                 psycopg.types.json.Jsonb(metadata)),
            )
            row = await cur.fetchone()
        return row[0]

    async def query_mistakes(
        self,
        project_id: str,
        query: str,
        top_k: int = 5,
        error_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """基于向量的错题检索

        使用 pgvector 的余弦距离进行相似度搜索。
        """
        conn = self._conn_or_raise()

        # 生成 query 向量
        vectors = await self._embed_fn([query])
        query_vector = vectors[0]
        # N-13：embedding 服务挂时 _default_embed 返回零向量，ORDER BY embedding <=> 零向量
        # 距离恒定→排序任意→L5 错题检索退化为随机。随机错题注入 prompt 比无错题更有害，故返回 []。
        if _is_zero_vector(query_vector):
            logger.warning(
                "[MEM] query_mistakes 查询向量为零向量(embedding 不可用)→返回空，避免随机错题排序"
            )
            return []
        vector_str = _vector_to_pg(query_vector)

        type_filter = ""
        params: list[Any] = [project_id]
        if error_type:
            type_filter = "AND error_type = %s"
            params.append(error_type)

        params.append(top_k)

        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, task_id, error_type, description, context,
                       fix_description, decay_weight, occurrence_count,
                       1 - (embedding <=> %s::vector) AS similarity,
                       last_seen_at, metadata_json
                FROM mem_mistakes
                WHERE project_id = %s {type_filter}
                  AND decay_weight > 0.05
                  AND COALESCE(metadata_json->>'status', '') NOT IN ('archived', 'dismissed')
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                [vector_str] + params[:1] + [vector_str] + params[1:],
            )
            rows = await cur.fetchall()

        return [
            {
                "id": r[0],
                "task_id": r[1],
                "error_type": r[2],
                "description": r[3],
                "context": r[4],
                "fix_description": r[5],
                "decay_weight": r[6],
                "occurrence_count": r[7],
                "similarity": float(r[8]) if r[8] is not None else 0.0,
                "last_seen_at": r[9],
                "metadata": r[10],
            }
            for r in rows
        ]

    async def increment_mistake_occurrence(self, mistake_id: int) -> None:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE mem_mistakes
                SET occurrence_count = occurrence_count + 1,
                    last_seen_at = now(),
                    decay_weight = LEAST(decay_weight + 0.1, 1.0)
                WHERE id = %s
                """,
                (mistake_id,),
            )

    async def get_all_mistakes(
        self, project_id: str, min_weight: float = 0.0
    ) -> list[dict[str, Any]]:
        """查询所有错题(用于衰减轮询)"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, error_type, description, decay_weight,
                       occurrence_count, last_seen_at
                FROM mem_mistakes
                WHERE project_id = %s AND decay_weight > %s
                ORDER BY decay_weight DESC
                """,
                (project_id, min_weight),
            )
            rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "error_type": r[1],
                "description": r[2],
                "decay_weight": float(r[3]),
                "occurrence_count": r[4],
                "last_seen_at": r[5],
            }
            for r in rows
        ]

    # ── L6: 成功模式集 ──────────────────────────

    async def write_success(
        self, project_id: str, entry: SuccessEntry
    ) -> int:
        """写入一条成功模式"""
        conn = self._conn_or_raise()

        embed_text = f"{entry.pattern_name}: {entry.description}"
        if entry.approach:
            embed_text += f" | {entry.approach}"
        vectors = await self._embed_fn([embed_text])
        vector = entry.embedding or vectors[0]

        # 检测零向量占位 → 在 metadata 中打标记
        metadata = dict(entry.metadata) if entry.metadata else {}
        if _is_zero_vector(vector):
            metadata["embedding_placeholder"] = True

        vector_str = _vector_to_pg(vector)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO mem_successes
                    (project_id, task_id, pattern_name, description,
                     approach, applicable_when, embedding, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
                RETURNING id
                """,
                (project_id, entry.task_id, entry.pattern_name, entry.description,
                 entry.approach, entry.applicable_when, vector_str,
                 psycopg.types.json.Jsonb(metadata)),
            )
            row = await cur.fetchone()
        return row[0]

    async def query_successes(
        self,
        project_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """基于向量的成功模式检索"""
        conn = self._conn_or_raise()

        vectors = await self._embed_fn([query])
        query_vector = vectors[0]
        # N-13：同 query_mistakes，零向量→相似度排序退化随机，L6 成功模式返回 [] 而非随机模式。
        if _is_zero_vector(query_vector):
            logger.warning(
                "[MEM] query_successes 查询向量为零向量(embedding 不可用)→返回空，避免随机成功模式排序"
            )
            return []
        vector_str = _vector_to_pg(query_vector)

        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, task_id, pattern_name, description,
                       approach, applicable_when, reuse_count,
                       1 - (embedding <=> %s::vector) AS similarity,
                       last_used_at, metadata_json
                FROM mem_successes
                WHERE project_id = %s
                  AND decay_weight > 0.05
                  AND COALESCE(metadata_json->>'status', '') NOT IN ('archived', 'dismissed')
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                [vector_str, project_id, vector_str, top_k],
            )
            rows = await cur.fetchall()

        return [
            {
                "id": r[0],
                "task_id": r[1],
                "pattern_name": r[2],
                "description": r[3],
                "approach": r[4],
                "applicable_when": r[5],
                "reuse_count": r[6],
                "similarity": float(r[7]) if r[7] is not None else 0.0,
                "last_used_at": r[8],
                "metadata": r[9],
            }
            for r in rows
        ]

    async def increment_success_reuse(self, success_id: int) -> None:
        """增加成功模式重用次数"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE mem_successes
                SET reuse_count = reuse_count + 1,
                    last_used_at = now()
                WHERE id = %s
                """,
                (success_id,),
            )

    async def dismiss_mistake(self, project_id: str, mistake_id: int) -> bool:
        """人工标记错题为已修复/归档 — 检索时降权排除。"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE mem_mistakes
                SET decay_weight = 0,
                    metadata_json = COALESCE(metadata_json, '{}'::jsonb)
                        || '{"status": "dismissed"}'::jsonb
                WHERE project_id = %s AND id = %s
                """,
                (project_id, mistake_id),
            )
            return cur.rowcount > 0

    async def mark_success_core(self, project_id: str, success_id: int, *, core: bool = True) -> bool:
        """标记成功模式为核心规则（metadata.core_rule）。"""
        conn = self._conn_or_raise()
        flag = "true" if core else "false"
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE mem_successes
                SET metadata_json = COALESCE(metadata_json, '{{}}'::jsonb)
                    || '{{"core_rule": {flag}}}'::jsonb
                WHERE project_id = %s AND id = %s
                """,
                (project_id, success_id),
            )
            return cur.rowcount > 0

    # ── 通用: 更新衰减权重(L5) ──────────────────

    async def update_mistake_decay_weight(
        self, mistake_id: int, new_weight: float
    ) -> None:
        """更新错题的衰减权重"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE mem_mistakes SET decay_weight = %s WHERE id = %s",
                (new_weight, mistake_id),
            )

    async def delete_expired_mistakes(self, min_weight: float = 0.05) -> int:
        """删除衰减到极低权重的错题"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM mem_mistakes WHERE decay_weight < %s",
                (min_weight,),
            )
            return cur.rowcount

    # ── 通用: 衰减权重(L6 成功模式) ─────────────

    async def get_all_successes(
        self, project_id: str, min_weight: float = 0.0
    ) -> list[dict[str, Any]]:
        """查询所有成功模式(用于衰减轮询)"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, pattern_name, description, decay_weight,
                       reuse_count, last_used_at
                FROM mem_successes
                WHERE project_id = %s AND decay_weight > %s
                ORDER BY decay_weight DESC
                """,
                (project_id, min_weight),
            )
            rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "pattern_name": r[1],
                "description": r[2],
                "decay_weight": float(r[3]),
                "reuse_count": r[4],
                "last_used_at": r[5],
            }
            for r in rows
        ]

    async def update_success_decay_weight(
        self, success_id: int, new_weight: float
    ) -> None:
        """更新成功模式的衰减权重"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE mem_successes SET decay_weight = %s WHERE id = %s",
                (new_weight, success_id),
            )

    async def delete_expired_successes(self, min_weight: float = 0.05) -> int:
        """删除衰减到极低权重的成功模式"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM mem_successes WHERE decay_weight < %s",
                (min_weight,),
            )
            return cur.rowcount


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _vector_to_pg(vector: list[float]) -> str:
    """将 Python list[float] 转为 pgvector 文本格式: '[0.1,0.2,...]'"""
    return "[" + ",".join(str(v) for v in vector) + "]"


def _is_zero_vector(vec: list[float] | None, sample_size: int = 4) -> bool:
    """检测向量是否为零向量(采样前几个元素即可)"""
    if vec is None:
        return True
    return all(v == 0.0 for v in vec[:sample_size]) and len(vec) > 0
