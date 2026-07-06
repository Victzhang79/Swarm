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


def _validate_embed_dim(embedding: list[float]) -> None:
    """B4 治本：写入前维度校验，对齐 KB(semantic_index)——换 embedding 模型致维度与
    L5/L6 表 DDL(vector(1024)) 不符时 fail-loud 抛异常，不静默 persisted:False（旧行为：
    PG 拒后被上层吞成"记忆丢失"无告警）。零向量占位本身是 1024 维，通过校验（另有零向量跳过逻辑）。"""
    n = len(embedding or [])
    if n != BGE_M3_DIMENSION:
        from swarm.knowledge.semantic_index import EmbeddingDimensionMismatchError
        raise EmbeddingDimensionMismatchError(
            f"记忆 embedding 维度 {n} 与表维度 {BGE_M3_DIMENSION} 不符，拒绝写入；"
            "请核对 embedding 模型与记忆表 DDL(BGE_M3_DIMENSION)"
        )

# ──────────────────────────────────────────────
# L5/L6 惰性衰减参数（单一事实源；decay.py 复用作默认）
# ──────────────────────────────────────────────
# WS1：衰减由“每日乘减 decay_weight”改为“读时按 last_seen_at 真实年龄现算”。
# decay_weight 语义 → 最近一次 seen/used 时的【基准/锚点权重】(anchor)，不随时间被乘减，
# 只在命中重振时刷新；时间流逝由 query 现算的 effective_weight 体现，摆脱调度器依赖。
L5_DECAY_FACTOR = 0.9        # 错题每日有效衰减因子(0.9=每天 -10%)
L6_DECAY_FACTOR = 0.95       # 成功模式每日有效衰减因子(更温和)
DECAY_DELETE_THRESHOLD = 0.05  # 有效权重低于此值视为已遗忘/物理清理

# WS2 近因排序地板：rank_score = max(similarity,0) * (FLOOR + (1-FLOOR)*recency_ratio)，
# recency_ratio = effective_weight/decay_weight = factor^(age/boost) ∈ (0,1]。
# 新鲜条目 age≈0 → ratio≈1 → 乘子=1.0 → 排序退化为纯余弦(向后兼容)；
# 陈旧近义 ratio→0 → 乘子→FLOOR，被新鲜同义压到后面。FLOOR=0.5：近因至多让语义分打 5 折，
# 不喧宾夺主(语义仍主导)，只在余弦接近时由近因破平/翻转。
RECENCY_RANK_FLOOR = 0.5

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
        logger.info("MemoryStore tables ensured")

    def _conn_or_raise(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("MemoryStore not connected — call connect() first")
        return self._conn

    def transaction(self):
        """A-P1-26：原子地把多次写包进单事务（连接为 autocommit，psycopg 会在块内
        显式 BEGIN/COMMIT，块中任一步失败则整体回滚）。

        用法:
            async with store.transaction():
                await store.write_success(...)
                await store.write_task_summary(...)
        """
        return self._conn_or_raise().transaction()

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

    # ── L2: 任务摘要 ────────────────────────────

    async def write_task_summary(
        self, project_id: str, summary: TaskSummary
    ) -> None:
        """写入任务摘要(自动维护滚动窗口)"""
        conn = self._conn_or_raise()
        # 复核 storage(L2 window) 治本：INSERT + 滚动窗口 DELETE 原 autocommit 分开提交，并发下窗口
        # 可能瞬时 >50 或误删。显式 conn.transaction() 让两句原子(嵌套调用即 savepoint，安全)。
        async with conn.transaction(), conn.cursor() as cur:
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

    async def summary_has_idempotency_key(self, project_id: str, idem_key: str) -> bool:
        """L2 摘要中是否已存在该幂等键(WS4：learn 重放去重，防二次写 + 双计数)。"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM mem_task_summary WHERE project_id = %s "
                "AND metadata_json->>'idempotency_key' = %s LIMIT 1",
                (project_id, idem_key),
            )
            return (await cur.fetchone()) is not None

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

        # 零向量占位(embedding 服务不可用且调用方未显式给向量)→ 跳过写入。
        # 对齐查询侧(query_mistakes:433 零向量直接返回空)：写入占位行只会污染
        # 相似度检索(ORDER BY embedding <=> 零向量 → 随机错题排序)，且服务恢复后这些行
        # 仍是零向量永远检索不到。tag-and-write 留垃圾，不如不写。
        if _is_zero_vector(embedding) and entry.embedding is None:
            logger.warning(
                "[MEM] write_mistake 跳过：embedding 为零向量占位(服务不可用)，不写入避免污染检索；error_type=%s",
                entry.error_type,
            )
            return -1

        # 检测零向量占位 → 在 metadata 中打标记便于排查
        metadata = dict(entry.metadata) if entry.metadata else {}
        if _is_zero_vector(embedding):
            metadata["embedding_placeholder"] = True

        # B4：写入前维度校验（fail-loud，不静默丢失）。零向量占位是 1024 维，通过。
        _validate_embed_dim(embedding)
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
        as_of: Any = None,
    ) -> list[dict[str, Any]]:
        """基于向量的错题检索

        使用 pgvector 的余弦距离进行相似度搜索；过滤/返回的 effective_weight 为
        WS1 惰性时间感知衰减——按 last_seen_at 到 as_of(默认 now())的真实年龄现算，
        摆脱后台 tick 调度依赖。as_of 仅供 eval 时间旅行，生产传 None。
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

        # 按 SQL 占位符出现顺序【显式】构造参数，杜绝拼接顺序错位。内层 SELECT 现算 effective_weight
        # (惰性衰减)，外层按 effective_weight 过滤；排序用 similarity DESC(== 旧 embedding<=> ASC，等价)。
        #   inner: similarity(vector) → effective_weight(factor, as_of) → where(project_id) → [type(error_type)]
        #   outer: where(threshold) → limit
        eff = _effective_weight_sql_l5()
        type_filter = ""
        sql_params: list[Any] = [vector_str, L5_DECAY_FACTOR, as_of, project_id]
        if error_type:
            type_filter = "AND error_type = %s"
            sql_params.append(error_type)
        sql_params.extend([DECAY_DELETE_THRESHOLD, RECENCY_RANK_FLOOR, RECENCY_RANK_FLOOR, top_k])

        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, task_id, error_type, description, context,
                       fix_description, decay_weight, occurrence_count,
                       similarity, last_seen_at, metadata_json, effective_weight
                FROM (
                    SELECT id, task_id, error_type, description, context,
                           fix_description, decay_weight, occurrence_count,
                           1 - (embedding <=> %s::vector) AS similarity,
                           last_seen_at, metadata_json,
                           {eff} AS effective_weight
                    FROM mem_mistakes
                    WHERE project_id = %s {type_filter}
                      AND COALESCE(metadata_json->>'status', '') NOT IN ('archived', 'dismissed', 'merged')
                ) sub
                WHERE effective_weight > %s
                ORDER BY GREATEST(similarity, 0) * (%s + (1.0 - %s)
                         * (effective_weight / GREATEST(decay_weight, 1e-6))) DESC,
                         similarity DESC
                LIMIT %s
                """,
                sql_params,
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
                "effective_weight": float(r[11]) if r[11] is not None else 0.0,
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
        self, project_id: str, min_weight: float = 0.0, as_of: Any = None
    ) -> list[dict[str, Any]]:
        """查询所有错题(用于衰减轮询)。

        decay_weight = 基准锚点权重；effective_weight = 按 last_seen_at 到 as_of 年龄现算的惰性衰减值。
        min_weight 仍按 base 过滤(粗筛保留所有未被永久 dismiss 的条目)，遗忘判定看 effective_weight。
        """
        conn = self._conn_or_raise()
        eff = _effective_weight_sql_l5()
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, error_type, description, decay_weight,
                       occurrence_count, last_seen_at, {eff} AS effective_weight
                FROM mem_mistakes
                WHERE project_id = %s AND decay_weight > %s
                  AND COALESCE(metadata_json->>'status', '') NOT IN ('archived', 'dismissed', 'merged')
                ORDER BY decay_weight DESC
                """,
                (L5_DECAY_FACTOR, as_of, project_id, min_weight),
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
                "effective_weight": float(r[6]) if r[6] is not None else 0.0,
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

        # 零向量占位 → 跳过写入(理由同 write_mistake：避免污染检索/留永久不可检索的垃圾行)。
        if _is_zero_vector(vector) and entry.embedding is None:
            logger.warning(
                "[MEM] write_success 跳过：embedding 为零向量占位(服务不可用)，不写入避免污染检索；pattern=%s",
                entry.pattern_name,
            )
            return -1

        # 检测零向量占位 → 在 metadata 中打标记
        metadata = dict(entry.metadata) if entry.metadata else {}
        if _is_zero_vector(vector):
            metadata["embedding_placeholder"] = True

        # B4：写入前维度校验（fail-loud，不静默丢失）。
        _validate_embed_dim(vector)
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
        query_vector: list[float] | None = None,
        as_of: Any = None,
    ) -> list[dict[str, Any]]:
        """基于向量的成功模式检索。

        query_vector：可选显式查询向量。传入则绕过 embed 服务（供测试用真实
        非零向量验证 decay/过滤逻辑，不依赖外部 bge-m3 服务，避免 CI 无服务时
        零向量短路返空导致的 CI-only 失败）。生产不传，走 _embed_fn 原路径。
        as_of：WS1 惰性衰减时间旅行(默认 now())；effective_weight 按 last_used_at 真实年龄现算。
        """
        conn = self._conn_or_raise()

        if query_vector is not None:
            query_vector = list(query_vector)
        else:
            vectors = await self._embed_fn([query])
            query_vector = vectors[0]
        # N-13：同 query_mistakes，零向量→相似度排序退化随机，L6 成功模式返回 [] 而非随机模式。
        if _is_zero_vector(query_vector):
            logger.warning(
                "[MEM] query_successes 查询向量为零向量(embedding 不可用)→返回空，避免随机成功模式排序"
            )
            return []
        vector_str = _vector_to_pg(query_vector)

        # 占位符顺序: similarity(vector) → effective_weight(factor, as_of) → where(project_id)
        #            → outer where(threshold) → limit
        eff = _effective_weight_sql_l6()
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, task_id, pattern_name, description,
                       approach, applicable_when, reuse_count,
                       similarity, last_used_at, metadata_json, effective_weight
                FROM (
                    SELECT id, task_id, pattern_name, description,
                           approach, applicable_when, reuse_count,
                           1 - (embedding <=> %s::vector) AS similarity,
                           last_used_at, metadata_json, decay_weight,
                           {eff} AS effective_weight
                    FROM mem_successes
                    WHERE project_id = %s
                      AND COALESCE(metadata_json->>'status', '') NOT IN ('archived', 'dismissed', 'merged')
                ) sub
                WHERE effective_weight > %s
                ORDER BY GREATEST(similarity, 0) * (%s + (1.0 - %s)
                         * (effective_weight / GREATEST(decay_weight, 1e-6))) DESC,
                         similarity DESC
                LIMIT %s
                """,
                [vector_str, L6_DECAY_FACTOR, as_of, project_id, DECAY_DELETE_THRESHOLD,
                 RECENCY_RANK_FLOOR, RECENCY_RANK_FLOOR, top_k],
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
                "effective_weight": float(r[10]) if r[10] is not None else 0.0,
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
        self, project_id: str, min_weight: float = 0.0, as_of: Any = None
    ) -> list[dict[str, Any]]:
        """查询所有成功模式(用于衰减轮询)。effective_weight 按 last_used_at 年龄现算(惰性)。"""
        conn = self._conn_or_raise()
        eff = _effective_weight_sql_l6()
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, pattern_name, description, decay_weight,
                       reuse_count, last_used_at, {eff} AS effective_weight
                FROM mem_successes
                WHERE project_id = %s AND decay_weight > %s
                  AND COALESCE(metadata_json->>'status', '') NOT IN ('archived', 'dismissed', 'merged')
                ORDER BY decay_weight DESC
                """,
                (L6_DECAY_FACTOR, as_of, project_id, min_weight),
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
                "effective_weight": float(r[6]) if r[6] is not None else 0.0,
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

def _effective_weight_sql_l5() -> str:
    """L5 有效权重 SQL 片段：base(decay_weight) * factor ^ (age_days / occurrence)。

    占位符顺序 = (factor, as_of)。age 用 GREATEST(..,0) 夹住，防 as_of 早于 last_seen_at
    时指数为负把权重反向放大到 base 之上(命中刚重振、时钟回拨等边角)。occurrence 越多衰减越慢，
    与旧 tick 公式 factor^(1/occ) 连乘 d 天 = factor^(d/occ) 完全一致(连续化)。
    """
    return (
        "decay_weight * POWER(%s, "
        "GREATEST(EXTRACT(EPOCH FROM (COALESCE(%s::timestamptz, now()) - last_seen_at)) / 86400.0, 0.0)"
        " / GREATEST(COALESCE(occurrence_count, 1), 1))"
    )


def _effective_weight_sql_l6() -> str:
    """L6 有效权重 SQL 片段：base * factor ^ (age_days / (reuse_count+1))。占位符顺序 = (factor, as_of)。"""
    return (
        "decay_weight * POWER(%s, "
        "GREATEST(EXTRACT(EPOCH FROM (COALESCE(%s::timestamptz, now()) - last_used_at)) / 86400.0, 0.0)"
        " / (COALESCE(reuse_count, 0) + 1))"
    )


def _vector_to_pg(vector: list[float]) -> str:
    """将 Python list[float] 转为 pgvector 文本格式: '[0.1,0.2,...]'"""
    return "[" + ",".join(str(v) for v in vector) + "]"


def _is_zero_vector(vec: list[float] | None, sample_size: int = 4) -> bool:
    """检测向量是否为零向量(采样前几个元素即可)"""
    if vec is None:
        return True
    return all(v == 0.0 for v in vec[:sample_size]) and len(vec) > 0
