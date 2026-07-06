"""PostgreSQL 持久化 — Project / TaskRecord / PreprocessProgress CRUD

使用 psycopg 同步模式（与 memory/store.py 一致的模式），
预处理管道中通过 asyncio.to_thread 包装调用。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# PG DDL
# ──────────────────────────────────────────────

PROJECTS_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'EMPTY',
    graph_status TEXT DEFAULT 'NONE',
    graph_progress REAL DEFAULT 0.0,
    graph_error TEXT,
    file_count INTEGER DEFAULT 0,
    symbol_count INTEGER DEFAULT 0,
    language_breakdown JSONB DEFAULT '{}',
    config JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""

TASK_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS task_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    description TEXT NOT NULL,
    status TEXT DEFAULT 'SUBMITTED',
    complexity TEXT,
    plan JSONB,
    subtask_count INTEGER DEFAULT 0,
    completed_subtasks INTEGER DEFAULT 0,
    abandoned_subtasks INTEGER DEFAULT 0,
    human_decision TEXT,
    merged_diff TEXT,
    thread_id TEXT,
    auto_accept BOOLEAN DEFAULT FALSE,
    queue_priority TEXT DEFAULT 'normal',
    base_commit TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_records_project ON task_records(project_id);
"""

# 任务审计日志（append-only，永不随项目/任务删除而清除）。
# 解决可追溯性盲区：task_records 被硬删后无任何痕迹，无法回答"那个任务是什么/
# 什么时候删的"。每次创建/状态变更/删除都在此留一条不可变记录。
TASK_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS task_audit_log (
    id SERIAL PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT,
    event TEXT NOT NULL,
    status TEXT,
    description TEXT,
    detail TEXT,
    at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_audit_task ON task_audit_log(task_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_task_audit_project ON task_audit_log(project_id, at DESC);
"""

PREPROCESS_PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS preprocess_progress (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    phase TEXT DEFAULT 'idle',
    phase_progress REAL DEFAULT 0.0,
    message TEXT DEFAULT '',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error TEXT,
    scan_stats JSONB DEFAULT '{}',
    index_stats JSONB DEFAULT '{}',
    embed_stats JSONB DEFAULT '{}',
    analysis_stats JSONB DEFAULT '{}'
);
"""

MILESTONE_REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS milestone_reports (
    id SERIAL PRIMARY KEY,
    project_id TEXT,
    phase TEXT NOT NULL,
    accept_rate REAL,
    threshold REAL,
    passed BOOLEAN,
    report JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_milestone_project ON milestone_reports(project_id, created_at DESC);
"""

# 应用内通知（持久化、可归档）。区别于 api/notify.py 的外部 webhook。
NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    task_id TEXT,
    project_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_archived ON notifications(archived, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_project ON notifications(project_id, created_at DESC);
"""

ALL_DDL = [PROJECTS_DDL, TASK_RECORDS_DDL, TASK_AUDIT_DDL, PREPROCESS_PROGRESS_DDL, MILESTONE_REPORTS_DDL, NOTIFICATIONS_DDL]

# 幂等列迁移（已有库 ADD COLUMN IF NOT EXISTS）
_TASK_RECORDS_MIGRATIONS = [
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS token_usage JSONB DEFAULT '{}'",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS duration_seconds REAL",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS merge_conflicts JSONB DEFAULT '[]'",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS l3_result JSONB DEFAULT '{}'",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS created_by_user_id TEXT",
    # Q4 规划子图：澄清/技术方案/评审产物（可追溯回看）
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS planning_artifacts JSONB DEFAULT '{}'",
    # B 部分：多模态摄取 —— 上传文件路径 + 「模型自行确认」选项 + 需求池模式
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS uploaded_files JSONB DEFAULT '[]'",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS auto_confirm_vision BOOLEAN DEFAULT FALSE",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS pooled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS ingest_draft TEXT DEFAULT ''",
    # round18 P2：进度三本账（完成/放弃/剩余）——放弃单元数,让 web 进度不再误导"卡在 X/N"。
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS abandoned_subtasks INTEGER DEFAULT 0",
]

_TASK_SELECT = """
    id, project_id, description, status, complexity,
    plan, subtask_count, completed_subtasks,
    human_decision, merged_diff, thread_id,
    token_usage, duration_seconds,
    merge_conflicts, l3_result, created_by_user_id,
    created_at, updated_at,
    uploaded_files, auto_confirm_vision, pooled, ingest_draft,
    abandoned_subtasks,
    auto_accept, queue_priority,
    base_commit
"""


# ──────────────────────────────────────────────
# 连接辅助
# ──────────────────────────────────────────────

def _get_conn_str(db_config: DatabaseConfig | None = None) -> str:
    """获取 PG 连接字符串（§3.2：委托 infra.db 单一来源，本地名保 seam）"""
    from swarm.infra.db import pg_conn_str
    return pg_conn_str(db_config)


def ensure_tables(conn_str: str | None = None) -> None:
    """同步建表（幂等）"""
    conn_str = conn_str or _get_conn_str()
    with psycopg.connect(conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            for ddl in ALL_DDL:
                cur.execute(ddl)
            for migration in _TASK_RECORDS_MIGRATIONS:
                cur.execute(migration)
    logger.info("ProjectStore tables ensured")


def _get_conn(conn_str: str | None = None):
    """获取池化连接的上下文管理器（autocommit）。

    用法不变：`with _get_conn(conn_str) as conn:` —— 退出时连接归还池而非关闭。
    """
    from swarm.infra.db import sync_pool

    return sync_pool(conn_str).connection()


# ~4 chars/token heuristic — billing-grade counts require LLM provider metadata
_CHARS_PER_TOKEN = 4


def estimate_token_usage(
    *,
    description: str = "",
    merged_diff: str = "",
    subtask_results: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """估算 token 用量（非精确计费；优先 subtask execution_log，否则 merged_diff + description）"""
    input_tokens = max(len(description or ""), 0) // _CHARS_PER_TOKEN
    output_tokens = 0

    if subtask_results:
        for output in subtask_results.values():
            if hasattr(output, "model_dump"):
                od: dict[str, Any] = output.model_dump()
            elif isinstance(output, dict):
                od = output
            else:
                od = {}
            output_tokens += len(od.get("execution_log") or "") // _CHARS_PER_TOKEN
            output_tokens += len(od.get("diff") or "") // _CHARS_PER_TOKEN
            output_tokens += len(od.get("summary") or "") // _CHARS_PER_TOKEN
    else:
        output_tokens = len(merged_diff or "") // _CHARS_PER_TOKEN

    total = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total": total,
        "estimate": True,
    }


def compute_task_duration_seconds(task: dict[str, Any]) -> float | None:
    """从 created_at 到当前时刻的任务耗时（秒）"""
    created = task.get("created_at")
    if created is None:
        return None
    if isinstance(created, str):
        text = created.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            created = datetime.fromisoformat(text)
        except ValueError:
            return None
    if not isinstance(created, datetime):
        return None
    now = datetime.now(timezone.utc) if created.tzinfo else datetime.now()
    if created.tzinfo and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max((now - created).total_seconds(), 0.0)


# ──────────────────────────────────────────────
# Project CRUD
# ──────────────────────────────────────────────

def create_project(
    project_id: str,
    name: str,
    path: str,
    description: str = "",
    config: dict[str, Any] | None = None,
    conn_str: str | None = None,
) -> dict[str, Any]:
    """创建项目，返回完整行"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects (id, name, path, description, config)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (path) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    config = COALESCE(projects.config, '{}'::jsonb) || COALESCE(EXCLUDED.config, '{}'::jsonb),
                    updated_at = NOW()
                RETURNING id, name, path, description, status, graph_status,
                          graph_progress, graph_error, file_count, symbol_count,
                          language_breakdown, config, created_at, updated_at
                """,
                (project_id, name, path, description, Jsonb(config or {})),
            )
            row = cur.fetchone()
    result = _row_to_project(row)
    # P1-23：path 是自然键——冲突时既存行的 id 无法重指（改 PK 会破坏 FK 引用），
    # 返回既存 id。但调用方传的新 id 被替换是重要事实，须可观测（不静默丢）。
    # config 已在 DO UPDATE 中 jsonb 并集合并（顶层键：新值覆盖同名、既存键保留）——
    # 不再静默丢弃调用方新 config。注：|| 是【浅合并】，同名顶层键下的嵌套对象整体替换。
    if result["id"] != project_id:
        logger.warning(
            "[project] create_project: path=%s 已存在(既存 id=%s)，传入的新 id=%s 被忽略、"
            "复用既存项目；config 已并集合并（未丢弃调用方新值）。",
            path, result["id"], project_id,
        )
    return result


def get_project(project_id: str, conn_str: str | None = None) -> dict[str, Any] | None:
    """按 ID 查询项目"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, path, description, status, graph_status,
                       graph_progress, graph_error, file_count, symbol_count,
                       language_breakdown, config, created_at, updated_at
                FROM projects WHERE id = %s
                """,
                (project_id,),
            )
            row = cur.fetchone()
    return _row_to_project(row) if row else None


def get_project_by_path(path: str, conn_str: str | None = None) -> dict[str, Any] | None:
    """按路径查询项目"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, path, description, status, graph_status,
                       graph_progress, graph_error, file_count, symbol_count,
                       language_breakdown, config, created_at, updated_at
                FROM projects WHERE path = %s
                """,
                (path,),
            )
            row = cur.fetchone()
    return _row_to_project(row) if row else None


def list_projects(conn_str: str | None = None) -> list[dict[str, Any]]:
    """列出所有项目"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, path, description, status, graph_status,
                       graph_progress, graph_error, file_count, symbol_count,
                       language_breakdown, config, created_at, updated_at
                FROM projects ORDER BY created_at DESC
                """
            )
            rows = cur.fetchall()
    return [_row_to_project(r) for r in rows]


def update_project(
    project_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    graph_status: str | None = None,
    graph_progress: float | None = None,
    graph_error: str | None = None,
    file_count: int | None = None,
    symbol_count: int | None = None,
    language_breakdown: dict[str, int] | None = None,
    config: dict[str, Any] | None = None,
    conn_str: str | None = None,
) -> dict[str, Any] | None:
    """部分更新项目字段"""
    sets: list[str] = []
    params: list[Any] = []

    if name is not None:
        sets.append("name = %s")
        params.append(name)
    if description is not None:
        sets.append("description = %s")
        params.append(description)
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if graph_status is not None:
        sets.append("graph_status = %s")
        params.append(graph_status)
    if graph_progress is not None:
        sets.append("graph_progress = %s")
        params.append(graph_progress)
    if graph_error is not None:
        sets.append("graph_error = %s")
        params.append(graph_error)
    if file_count is not None:
        sets.append("file_count = %s")
        params.append(file_count)
    if symbol_count is not None:
        sets.append("symbol_count = %s")
        params.append(symbol_count)
    if language_breakdown is not None:
        sets.append("language_breakdown = %s")
        params.append(Jsonb(language_breakdown))
    if config is not None:
        # C4：与 create 的 jsonb || 语义一致——顶层键并集(新值覆盖同名、其余既存键保留)，
        # 不整列覆盖(原 config = %s 会清掉未在本次传入的既存键)。SQL 级合并且原子，免调用方
        # 读-改-写(planning_nodes DETECT_STACK 缓存即此场景，有 lost-update 竞态)。
        sets.append("config = COALESCE(config, '{}'::jsonb) || %s")
        params.append(Jsonb(config))

    if not sets:
        return get_project(project_id, conn_str)

    sets.append("updated_at = NOW()")
    params.append(project_id)

    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE projects SET {', '.join(sets)}
                WHERE id = %s
                RETURNING id, name, path, description, status, graph_status,
                          graph_progress, graph_error, file_count, symbol_count,
                          language_breakdown, config, created_at, updated_at
                """,
                params,
            )
            row = cur.fetchone()
    return _row_to_project(row) if row else None


def _delete_if_table_exists(cur: Any, table: str, project_id: str) -> None:
    """按 project_id 删除某表行；表不存在则跳过（兼容未启用某些子系统的部署）。

    用 to_regclass 预检，避免因表缺失抛错而回滚整个级联删除事务（12.5）。
    table 来自固定白名单常量，非用户输入，无注入风险。
    """
    cur.execute("SELECT to_regclass(%s)", (table,))
    row = cur.fetchone()
    if not row or row[0] is None:
        return
    cur.execute(f"DELETE FROM {table} WHERE project_id = %s", (project_id,))


def delete_project(project_id: str, conn_str: str | None = None) -> bool:
    """删除项目及其关联数据（task_records + preprocess_progress 级联删除需手动）。

    删除前把该项目所有任务写入 append-only 审计日志，保证可追溯（避免再次发生
    "任务被删后无任何痕迹、无法回答删了什么"的问题）。
    """
    # 先快照所有任务写审计
    try:
        for t in list_tasks(project_id, conn_str):
            append_task_audit(
                t.get("id"),
                event="deleted_with_project",
                project_id=project_id,
                status=t.get("status"),
                description=t.get("description"),
                detail="cascade delete via delete_project",
                conn_str=conn_str,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除项目 %s 前写任务审计失败: %s", project_id, exc)

    with _get_conn(conn_str) as conn:
        # A-P1-23 修复：池连接是 autocommit，每条 DELETE 各自落库，原注释「全部在同一
        # 事务内、要么全删要么回滚」是假话——中途任一句失败会留下半删的孤立数据。
        # psycopg 在 autocommit 连接上也支持 conn.transaction()：它会显式 BEGIN，块内
        # 所有 DELETE 在一个事务里，块正常退出才 COMMIT，抛异常则整体 ROLLBACK，
        # 真正做到级联删除原子化。Qdrant 向量在路由层事务外 best-effort 清理。
        with conn.transaction():
            with conn.cursor() as cur:
                # 级联删除该项目所有关联数据（修复 12.5：此前仅删 task_records +
                # preprocess_progress + projects，残留 kb_*/mem_* 行成为孤立数据，长期膨胀）。
                cur.execute("DELETE FROM task_records WHERE project_id = %s", (project_id,))
                cur.execute("DELETE FROM preprocess_progress WHERE project_id = %s", (project_id,))
                # 知识库 Layer A/C/D + 增量队列
                for tbl in (
                    "kb_file_index",
                    "kb_symbol_index",
                    "kb_dependency_graph",
                    "kb_norms",
                    "kb_modification_log",
                    "kb_co_occurrence",
                    "kb_update_events",
                    "kb_pending_embeddings",
                ):
                    _delete_if_table_exists(cur, tbl, project_id)
                # 记忆 L2/L5/L6（向量随行一并删；按 project_id 列）
                for tbl in (
                    "mem_task_summary",
                    "mem_mistakes",
                    "mem_successes",
                ):
                    _delete_if_table_exists(cur, tbl, project_id)
                # L1 用户画像 mem_user_profile 特殊：PRIMARY KEY 是 user_id，项目维度编码在【复合键】
                # user_id = f"{user}:{project_id}"（全局画像用 "{user}:__global__"，见 auth.profile_key）。
                # auth 迁移 ALTER 加的 project_id 列 4 处写入全不填(恒 '')——按它删既【匹配 0 行】(级联名存
                # 实亡)、又在【迁移未跑=列不存在时 undefined_column 回滚整个级联事务→项目根本删不掉】。故不碰
                # 那个列，改按复合键尾段删：清该项目下每用户的 L1 画像行；":__global__" 全局画像尾段不同→不
                # 匹配→保留。用 right() 精确尾段比较（非 LIKE），project_id 内含 %/_ 也不会误伤。
                cur.execute("SELECT to_regclass('mem_user_profile')")
                _r = cur.fetchone()
                if _r and _r[0] is not None and project_id:
                    cur.execute(
                        "DELETE FROM mem_user_profile WHERE right(user_id, %s) = %s",
                        (len(project_id) + 1, f":{project_id}"),
                    )
                # P2-A：此前遗漏的项目级作用域表 → 补齐级联，杜绝孤立行长期膨胀。
                # （task_audit_log 【故意保留】：append-only 追溯"删了什么"，由 TTL purge 兜底。）
                for tbl in (
                    "milestone_reports",
                    "notifications",
                    "llm_token_usage",
                ):
                    _delete_if_table_exists(cur, tbl, project_id)
                cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
                deleted = cur.rowcount
    return deleted > 0


def count_tasks_by_status(conn_str: str | None = None) -> dict[str, int]:
    """P2-D：按 status 聚合任务数（供 /metrics 导出）。DB 不可用返回空 dict（非致命）。"""
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, COUNT(*) FROM task_records GROUP BY status")
                return {str(row[0]): int(row[1]) for row in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[METRICS] count_tasks_by_status 失败(非致命): %s", exc)
        return {}


def purge_old_task_audit(retention_days: int = 180, conn_str: str | None = None) -> int:
    """P2-A：裁剪 append-only 的 task_audit_log —— 保留最近 retention_days 天，删更早行。

    task_audit_log 是纯 append（每 audit 事件一行 + delete_project 快照），无任何删除路径 →
    生产长跑持续膨胀。保留窗口内（默认 180 天）足够追溯与合规，更早的按 TTL 裁掉。
    retention_days<=0 视为关闭（返回 0，不删）。返回删除行数。幂等、可反复跑。"""
    if retention_days <= 0:
        return 0
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM task_audit_log WHERE at < NOW() - make_interval(days => %s)",
                    (int(retention_days),),
                )
                return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("[PURGE] task_audit_log 裁剪失败(非致命): %s", exc)
        return 0


# ──────────────────────────────────────────────
# TaskRecord CRUD
# ──────────────────────────────────────────────

def create_task(
    task_id: str,
    project_id: str,
    description: str,
    created_by_user_id: str | None = None,
    uploaded_files: list[str] | None = None,
    auto_confirm_vision: bool = False,
    pooled: bool = False,
    conn_str: str | None = None,
) -> dict[str, Any]:
    """创建任务记录。

    uploaded_files: B 部分上传的文件路径（任务专属目录，绝对路径）。
    auto_confirm_vision: 用户勾选「模型自行确认」→ 跳过图片理解人工确认。
    pooled: True=仅入需求池（不立即执行），False=立即执行。
    """
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO task_records (
                    id, project_id, description, created_by_user_id,
                    uploaded_files, auto_confirm_vision, pooled
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {_TASK_SELECT}
                """,
                (task_id, project_id, description, created_by_user_id,
                 Jsonb(uploaded_files or []), auto_confirm_vision, pooled),
            )
            row = cur.fetchone()
    task = _row_to_task(row)
    # 创建即留痕（append-only 审计）
    append_task_audit(
        task_id, event="created", project_id=project_id,
        status=task.get("status") if task else "SUBMITTED",
        description=description, conn_str=conn_str,
    )
    return task


def find_active_duplicate_task(
    project_id: str,
    description: str,
    conn_str: str | None = None,
) -> dict[str, Any] | None:
    """查找同项目内「描述相同且仍在进行（非终态）」的任务，用于创建去重。

    终态(DONE/FAILED/CANCELLED)不算重复——允许对历史任务重新发起。
    描述做 trim + 大小写无关比较。无匹配返回 None。
    """
    desc = (description or "").strip()
    if not desc:
        return None
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_TASK_SELECT}
                FROM task_records
                WHERE project_id = %s
                  AND LOWER(BTRIM(description)) = LOWER(%s)
                  AND status <> ALL(%s)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id, desc, list(_TERMINAL_STATUSES)),
            )
            row = cur.fetchone()
    return _row_to_task(row) if row else None


def get_task(task_id: str, conn_str: str | None = None) -> dict[str, Any] | None:
    """按 ID 查询任务"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_TASK_SELECT}
                FROM task_records WHERE id = %s
                """,
                (task_id,),
            )
            row = cur.fetchone()
    return _row_to_task(row) if row else None


def save_planning_artifacts(
    task_id: str, artifacts: dict[str, Any], conn_str: str | None = None
) -> bool:
    """持久化 Q4 规划产物（澄清历史/技术方案/评审决策）到 task_records.planning_artifacts。

    可追溯(F)：任务详情页可回看"当初为何这么规划"。失败不抛出（非关键路径）。
    """
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE task_records SET planning_artifacts = %s WHERE id = %s",
                    (json.dumps(artifacts, ensure_ascii=False), task_id),
                )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("save_planning_artifacts failed: %s", exc)
        return False


def get_planning_artifacts(task_id: str, conn_str: str | None = None) -> dict[str, Any]:
    """读取任务的规划产物。无则返回空 dict。"""
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT planning_artifacts FROM task_records WHERE id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
        if not row or not row[0]:
            return {}
        val = row[0]
        return val if isinstance(val, dict) else json.loads(val)
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_planning_artifacts failed: %s", exc)
        return {}


def list_tasks(project_id: str, conn_str: str | None = None) -> list[dict[str, Any]]:
    """列出项目下所有任务"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_TASK_SELECT}
                FROM task_records
                WHERE project_id = %s
                ORDER BY created_at DESC
                """,
                (project_id,),
            )
            rows = cur.fetchall()
    return [_row_to_task(r) for r in rows]


def list_orphan_candidates(conn_str: str | None = None) -> list[dict[str, Any]]:
    """列出全库（跨项目）处于"进行中"（非终态、非 POOLED）的任务。

    P0-A：API/leader 重启后，这些任务的进程内执行态已清零，可能 orphaned。
    reconcile_orphan_tasks 据此按态类别分治（中断挂起态保留 / SUBMITTED 重入队 /
    活跃执行态 fail-closed）。参数用 task_states.ACTIVE_DB_STATUSES 单一事实源。
    """
    from swarm.task_states import ACTIVE_DB_STATUSES

    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_TASK_SELECT}
                FROM task_records
                WHERE status = ANY(%s)
                ORDER BY created_at ASC
                """,
                (list(ACTIVE_DB_STATUSES),),
            )
            rows = cur.fetchall()
    return [_row_to_task(r) for r in rows]


def update_task(
    task_id: str,
    *,
    status: str | None = None,
    complexity: str | None = None,
    plan: dict[str, Any] | None = None,
    subtask_count: int | None = None,
    completed_subtasks: int | None = None,
    abandoned_subtasks: int | None = None,
    human_decision: str | None = None,
    merged_diff: str | None = None,
    thread_id: str | None = None,
    token_usage: dict[str, Any] | None = None,
    duration_seconds: float | None = None,
    merge_conflicts: list[dict[str, Any]] | None = None,
    l3_result: dict[str, Any] | None = None,
    auto_accept: bool | None = None,
    queue_priority: str | None = None,
    base_commit: str | None = None,
    conn_str: str | None = None,
) -> dict[str, Any] | None:
    """部分更新任务字段"""
    sets: list[str] = []
    params: list[Any] = []

    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if complexity is not None:
        sets.append("complexity = %s")
        params.append(complexity)
    if plan is not None:
        sets.append("plan = %s")
        params.append(Jsonb(plan))
    if subtask_count is not None:
        sets.append("subtask_count = %s")
        params.append(subtask_count)
    if completed_subtasks is not None:
        sets.append("completed_subtasks = %s")
        params.append(completed_subtasks)
    if abandoned_subtasks is not None:
        sets.append("abandoned_subtasks = %s")
        params.append(abandoned_subtasks)
    if human_decision is not None:
        sets.append("human_decision = %s")
        params.append(human_decision)
    if merged_diff is not None:
        sets.append("merged_diff = %s")
        params.append(merged_diff)
    if thread_id is not None:
        sets.append("thread_id = %s")
        params.append(thread_id)
    if token_usage is not None:
        sets.append("token_usage = %s")
        params.append(Jsonb(token_usage))
    if duration_seconds is not None:
        sets.append("duration_seconds = %s")
        params.append(duration_seconds)
    if merge_conflicts is not None:
        sets.append("merge_conflicts = %s")
        params.append(Jsonb(merge_conflicts))
    if l3_result is not None:
        sets.append("l3_result = %s")
        params.append(Jsonb(l3_result))
    if auto_accept is not None:
        sets.append("auto_accept = %s")
        params.append(auto_accept)
    if queue_priority is not None:
        sets.append("queue_priority = %s")
        params.append(queue_priority)
    if base_commit is not None:
        # 复核 L-3：哨兵是【is not None】非 truthiness——retry_task 用 base_commit="" 清空以令 run_task
        # 重捕获新基线；若改成 `if base_commit:` 会跳过空串写入 → retry 静默沿用旧 birth base。勿"优化"。
        sets.append("base_commit = %s")
        params.append(base_commit)

    if not sets:
        return get_task(task_id, conn_str)

    sets.append("updated_at = NOW()")
    params.append(task_id)

    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE task_records SET {', '.join(sets)}
                WHERE id = %s
                RETURNING {_TASK_SELECT}
                """,
                params,
            )
            row = cur.fetchone()
    return _row_to_task(row) if row else None


def claim_human_gate(
    task_id: str,
    allowed_states: "frozenset[str] | set[str] | tuple[str, ...]",
    new_status: str,
    *,
    human_decision: str | None = None,
    conn_str: str | None = None,
) -> dict[str, Any] | None:
    """原子认领人工闸决策（P1-A 审批幂等 + 前置态校验）。

    单条条件 UPDATE = 原子（PG 行锁）：仅当任务【当前处于 allowed_states 之一】才把状态推进到
    new_status（并可选记 human_decision），返回更新后的行；否则（状态不匹配 / 已被并发认领离开该态 /
    已终态）返回 None。

    这解决双击/重复提交：第一次点击把状态推出人工闸态，第二次点击的同一 UPDATE 因 WHERE 不再匹配
    → 0 行 → None → 端点走幂等无副作用分支（不重复 apply diff / 不重复触发 resume / 不发spurious）。
    """
    sets = ["status = %s", "updated_at = NOW()"]
    params: list[Any] = [new_status]
    if human_decision is not None:
        sets.append("human_decision = %s")
        params.append(human_decision)
    params.append(task_id)
    params.append(list(allowed_states))
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE task_records SET {', '.join(sets)}
                WHERE id = %s AND status = ANY(%s)
                RETURNING {_TASK_SELECT}
                """,
                params,
            )
            row = cur.fetchone()
    return _row_to_task(row) if row else None


def append_task_audit(
    task_id: str,
    event: str,
    *,
    project_id: str | None = None,
    status: str | None = None,
    description: str | None = None,
    detail: str | None = None,
    conn_str: str | None = None,
) -> None:
    """向 append-only 审计日志写一条记录（永不随删除清除，保证可追溯）。

    容错：审计失败不应阻断主流程（best-effort）。
    """
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO task_audit_log (task_id, project_id, event, status, description, detail)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (task_id, project_id, event, status, description, detail),
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("写任务审计日志失败 task=%s event=%s: %s", task_id, event, exc)


def list_task_audit(
    task_id: str | None = None,
    project_id: str | None = None,
    limit: int = 100,
    conn_str: str | None = None,
    project_ids: "list[str] | None" = None,
) -> list[dict[str, Any]]:
    """查询任务审计日志（按时间倒序）。

    #5(a)：project_ids 非 None 时把结果限定在这批项目内（成员项目 scope，非 admin 越权防护）。
    空列表 = 无任何可见项目 → fail-closed 返回空（绝不因"无过滤"而泄露全库）。
    """
    if project_ids is not None and len(project_ids) == 0:
        return []
    conditions: list[str] = []
    params: list[Any] = []
    if task_id:
        conditions.append("task_id = %s")
        params.append(task_id)
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    if project_ids is not None:
        conditions.append("project_id = ANY(%s)")
        params.append(list(project_ids))
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, task_id, project_id, event, status, description, detail, at "
                f"FROM task_audit_log{where} ORDER BY at DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def delete_task(task_id: str, conn_str: str | None = None) -> bool:
    """删除任务（删除前在 append-only 审计日志留痕，保证可追溯）。"""
    # 先抓取任务快照写审计（删除后就查不到了）
    snap = get_task(task_id, conn_str)
    if snap is not None:
        append_task_audit(
            task_id,
            event="deleted",
            project_id=snap.get("project_id"),
            status=snap.get("status"),
            description=snap.get("description"),
            detail="task_records hard-deleted",
            conn_str=conn_str,
        )
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM task_records WHERE id = %s", (task_id,))
            deleted = cur.rowcount
    return deleted > 0


# ──────────────────────────────────────────────
# Task stats & notifications (Phase 5)
# ──────────────────────────────────────────────

# #3 round22：PARTIAL 是终态（任务已收敛不再推进），历史漏它 → 去重(:549)误当活跃任务、
# 平均时长(:925)统计漏 PARTIAL。与 types.TaskStatus.is_terminal_status 同口径（含 PARTIAL）。
# 单一事实源见 swarm/task_states.py（保 tuple 形态，下游 SQL list(...) 调用点不动）。
from swarm.task_states import (  # noqa: E402
    INTERRUPT_SUSPENDED_STATES as _INTERRUPT_SUSPENDED_STATES,
    TERMINAL_STATES as _TERMINAL_STATES_SET,
)
_TERMINAL_STATUSES = tuple(sorted(_TERMINAL_STATES_SET))
# 需通知的状态：完成/失败 + 全部人工闸中断挂起态（含 CLARIFYING/DESIGN_REVIEW）。
# 单一事实源：否则新增的中断态用户收不到通知 → 人工闸静默死等（与 P0-D 死区同源）。
_NOTIFY_STATUSES = ("DONE", "FAILED", *sorted(_INTERRUPT_SUSPENDED_STATES))


def _task_event_type(status: str) -> str:
    if status == "DONE":
        return "task_completed"
    if status == "FAILED":
        return "task_failed"
    if status in _INTERRUPT_SUSPENDED_STATES:
        return "waiting_review"
    return "task_updated"


def _serialize_dt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _get_learning_effectiveness(
    project_id: str,
    conn_str: str | None = None,
) -> dict[str, Any]:
    """学习趋势：综合 mem_mistakes（错题）与 mem_successes（成功模式）。

    趋势判定（优先级从上到下）：
      - improving: 近30天错题数 < 前30天，或（无历史错题但已积累成功模式）
      - stable:    近30天错题数 与前30天持平
      - regressing: 近30天错题数 > 前30天
      - learning:  尚无任何错题但已有成功模式（健康冷启动）
      - unknown:   完全无数据（错题+成功都为 0）
    """
    default: dict[str, Any] = {
        "recent_mistakes": 0,
        "prior_mistakes": 0,
        "recent_successes": 0,
        "total_successes": 0,
        "trend": "unknown",
    }
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM mem_mistakes
                    WHERE project_id = %s
                      AND created_at >= NOW() - INTERVAL '30 days'
                    """,
                    (project_id,),
                )
                recent = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT COUNT(*) FROM mem_mistakes
                    WHERE project_id = %s
                      AND created_at >= NOW() - INTERVAL '60 days'
                      AND created_at < NOW() - INTERVAL '30 days'
                    """,
                    (project_id,),
                )
                prior = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT COUNT(*) FROM mem_successes
                    WHERE project_id = %s
                      AND created_at >= NOW() - INTERVAL '30 days'
                    """,
                    (project_id,),
                )
                recent_succ = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT COUNT(*) FROM mem_successes WHERE project_id = %s",
                    (project_id,),
                )
                total_succ = int(cur.fetchone()[0])
    except Exception as exc:
        logger.debug("learning_effectiveness unavailable: %s", exc)
        return default

    # 趋势判定：错题变化为主信号，成功模式作为「健康冷启动」补充
    if recent == 0 and prior == 0 and total_succ == 0:
        trend = "unknown"          # 完全无数据
    elif recent == 0 and prior == 0 and total_succ > 0:
        trend = "learning"         # 无错题、有成功积累 → 健康
    elif recent < prior:
        trend = "improving"        # 错题减少
    elif recent > prior:
        trend = "regressing"       # 错题增多
    else:
        trend = "stable"           # 持平

    return {
        "recent_mistakes": recent,
        "prior_mistakes": prior,
        "recent_successes": recent_succ,
        "total_successes": total_succ,
        "trend": trend,
    }


def get_task_stats(project_id: str | None = None, conn_str: str | None = None,
                   *, project_ids: "set[str] | None" = None) -> dict[str, Any]:
    """聚合任务统计；可选按 project_id（单项目）或 project_ids（成员项目白名单）过滤。

    C2 治本：无 project_id 时旧代码聚合【全库】任务（含最近 10 条跨项目 description/token_usage）→
    任意登录用户跨项目泄露。调用方(get_stats)对非 admin 传 project_ids=成员项目集，此处限定范围；
    空集 → ANY('{}') 命中 0 行 → 全 0（无可见项目，如实空）。project_ids=None 表示不限（admin/单项目）。
    """
    if project_id:
        where = "WHERE project_id = %s"
        params: tuple[Any, ...] = (project_id,)
    elif project_ids is not None:
        where = "WHERE project_id = ANY(%s)"
        params = (list(project_ids),)
    else:
        where = ""
        params = ()

    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_tasks,
                    COUNT(*) FILTER (WHERE status = 'DONE') AS completed,
                    COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                    COUNT(*) FILTER (WHERE status = 'CANCELLED') AS cancelled,
                    COUNT(*) FILTER (WHERE status = 'PARTIAL') AS partial,
                    COUNT(*) FILTER (
                        WHERE status = 'DONE'
                          AND UPPER(COALESCE(human_decision, '')) = 'ACCEPT'
                    ) AS approved
                FROM task_records
                {where}
                """,
                params,
            )
            counts = cur.fetchone()
            total, completed, failed, cancelled, partial, approved = counts

            terminal_where = where
            terminal_params: list[Any] = list(params)
            if terminal_where:
                terminal_where += " AND status = ANY(%s)"
            else:
                terminal_where = "WHERE status = ANY(%s)"
            terminal_params.append(list(_TERMINAL_STATUSES))

            cur.execute(
                f"""
                SELECT AVG(EXTRACT(EPOCH FROM (updated_at - created_at)))
                FROM task_records
                {terminal_where}
                """,
                terminal_params,
            )
            avg_row = cur.fetchone()
            avg_duration = float(avg_row[0]) if avg_row and avg_row[0] is not None else None

            cur.execute(
                f"""
                SELECT
                    COALESCE(SUM((token_usage->>'total')::bigint), 0),
                    AVG((token_usage->>'total')::float)
                        FILTER (WHERE (token_usage->>'total') IS NOT NULL)
                FROM task_records
                {where}
                """,
                params,
            )
            token_row = cur.fetchone()
            total_tokens = int(token_row[0]) if token_row and token_row[0] is not None else 0
            avg_tokens_raw = token_row[1] if token_row else None
            avg_tokens = round(float(avg_tokens_raw), 1) if avg_tokens_raw is not None else None

            cur.execute(
                f"""
                SELECT id, project_id, description, status, human_decision,
                       created_at, updated_at,
                       COALESCE(duration_seconds,
                                EXTRACT(EPOCH FROM (updated_at - created_at))) AS duration_seconds,
                       token_usage
                FROM task_records
                {where}
                ORDER BY updated_at DESC
                LIMIT 10
                """,
                params,
            )
            recent_rows = cur.fetchall()

    accept_rate = round(approved / completed, 4) if completed else None
    # 合并率/成功率：DONE 占所有终态任务的比例。#3 round22：PARTIAL 也是终态且【非成功】，
    # 必须计入分母（否则部分交付被统计学"洗白"——既不进分子也不进分母，merge_rate 虚高）。
    # partial 单列一个类目（既非 completed 也非 failed），如实反映"诚实未完成"占比。
    terminal_total = (completed or 0) + (failed or 0) + (cancelled or 0) + (partial or 0)
    merge_rate = round(completed / terminal_total, 4) if terminal_total else None

    recent_tasks = [
        {
            "id": row[0],
            "project_id": row[1],
            "description": row[2],
            "status": row[3],
            "human_decision": row[4],
            "created_at": _serialize_dt(row[5]),
            "updated_at": _serialize_dt(row[6]),
            "duration_seconds": round(float(row[7]), 2) if row[7] is not None else None,
            "token_usage": _parse_token_usage(row[8]),
        }
        for row in recent_rows
    ]

    result: dict[str, Any] = {
        "total_tasks": total,
        "completed": completed,
        "failed": failed,
        "cancelled": cancelled,
        "partial": partial,
        "approved": approved,
        "accept_rate": accept_rate,
        "merge_rate": merge_rate,
        "avg_duration_seconds": round(avg_duration, 2) if avg_duration is not None else None,
        "total_tokens": total_tokens,
        "avg_tokens": avg_tokens,
        "recent_tasks": recent_tasks,
    }

    if project_id:
        result["learning_effectiveness"] = _get_learning_effectiveness(project_id, conn_str)

    return result


def get_task_notifications(
    project_id: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
    conn_str: str | None = None,
) -> list[dict[str, Any]]:
    """最近任务状态变更事件（MVP：查终态/待审任务）"""
    conditions = ["status = ANY(%s)"]
    params: list[Any] = [list(_NOTIFY_STATUSES)]

    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    if since is not None:
        conditions.append("updated_at > %s")
        params.append(since)

    params.append(limit)

    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, project_id, description, status, human_decision, updated_at
                FROM task_records
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

    notifications: list[dict[str, Any]] = []
    for row in rows:
        status = row[3]
        event_type = _task_event_type(status)
        notifications.append(
            {
                "task_id": row[0],
                "project_id": row[1],
                "description": row[2],
                "status": status,
                "human_decision": row[4],
                "event_type": event_type,
                "updated_at": _serialize_dt(row[5]),
                "message": _notification_message(status, row[2]),
            }
        )
    return notifications


def _notification_message(status: str, description: str) -> str:
    short = (description or "")[:80]
    if status == "DONE":
        return f"任务已完成: {short}"
    if status == "FAILED":
        return f"任务失败: {short}"
    if status in _INTERRUPT_SUSPENDED_STATES:
        return f"待审核: {short}"
    return short


# ──────────────────────────────────────────────
# 应用内通知 CRUD（持久化 notifications 表）
# ──────────────────────────────────────────────

# 通知创建后的回调钩子（解耦：store 不依赖 httpx/asyncio）。
# app.py 启动时注册一个 hook，把新通知转发给 api.notify.dispatch_notification。
_notification_hooks: list = []


def register_notification_hook(fn) -> None:
    """注册一个 "通知已创建" 回调。fn(record: dict) 同步调用，内部自行调度异步推送。"""
    if fn not in _notification_hooks:
        _notification_hooks.append(fn)


def _fire_notification_hooks(record: dict) -> None:
    """触发所有已注册 hook。任一失败不影响通知写入（非关键路径）。"""
    for fn in list(_notification_hooks):
        try:
            fn(record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("notification hook failed: %s", exc)


def create_notification(
    event_type: str,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    title: str = "",
    message: str = "",
    conn_str: str | None = None,
) -> dict[str, Any]:
    """写入一条应用内通知，返回完整记录。失败不抛出（通知非关键路径）。"""
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO notifications
                        (event_type, task_id, project_id, title, message)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, event_type, task_id, project_id,
                              title, message, archived, created_at
                    """,
                    (event_type, task_id, project_id, title, message),
                )
                row = cur.fetchone()
        record = _row_to_notification(row)
        _fire_notification_hooks(record)
        return record
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_notification failed: %s", exc)
        return {}


def list_notifications(
    *,
    project_id: str | None = None,
    project_ids: "Iterable[str] | None" = None,
    include_archived: bool = False,
    limit: int = 50,
    conn_str: str | None = None,
) -> list[dict[str, Any]]:
    """列出通知，默认只返回未归档，按时间倒序。

    #19：project_ids（可访问项目白名单）用于非 admin 调用方未指定单一 project_id 时，
    把结果限定在用户有权访问的项目内（防跨项目 IDOR 读取）。空集合→不返回任何记录。
    """
    conditions: list[str] = []
    params: list[Any] = []
    if not include_archived:
        conditions.append("archived = FALSE")
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    elif project_ids is not None:
        _ids = list(project_ids)
        if not _ids:
            return []
        conditions.append("project_id = ANY(%s)")
        params.append(_ids)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, event_type, task_id, project_id,
                       title, message, archived, created_at
                FROM notifications
                {where}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
    return [_row_to_notification(r) for r in rows]


def count_unread_notifications(
    *,
    project_id: str | None = None,
    project_ids: "Iterable[str] | None" = None,
    conn_str: str | None = None,
) -> int:
    """未归档通知数（铃铛绿点用）。project_ids 见 list_notifications（#19 防跨项目计数泄露）。"""
    conditions = ["archived = FALSE"]
    params: list[Any] = []
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    elif project_ids is not None:
        _ids = list(project_ids)
        if not _ids:
            return 0
        conditions.append("project_id = ANY(%s)")
        params.append(_ids)
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM notifications WHERE {' AND '.join(conditions)}",
                params,
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


def get_notification_project_id(
    notification_id: int,
    conn_str: str | None = None,
) -> tuple[bool, str | None]:
    """#19：取单条通知的 project_id（供 archive 鉴权用）。返回 (存在?, project_id)。"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT project_id FROM notifications WHERE id = %s",
                (notification_id,),
            )
            row = cur.fetchone()
    if not row:
        return False, None
    return True, row[0]


def archive_notification(
    notification_id: int,
    conn_str: str | None = None,
) -> bool:
    """归档单条通知，返回是否命中。"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notifications SET archived = TRUE WHERE id = %s AND archived = FALSE",
                (notification_id,),
            )
            return cur.rowcount > 0


def archive_all_notifications(
    *,
    project_id: str | None = None,
    project_ids: "Iterable[str] | None" = None,
    conn_str: str | None = None,
) -> int:
    """归档全部（可选按项目过滤），返回归档条数。project_ids 见 list_notifications（#19）。"""
    conditions = ["archived = FALSE"]
    params: list[Any] = []
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    elif project_ids is not None:
        _ids = list(project_ids)
        if not _ids:
            return 0
        conditions.append("project_id = ANY(%s)")
        params.append(_ids)
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE notifications SET archived = TRUE WHERE {' AND '.join(conditions)}",
                params,
            )
            return cur.rowcount


def _row_to_notification(row: Any) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "id": row[0],
        "event_type": row[1],
        "task_id": row[2],
        "project_id": row[3],
        "title": row[4],
        "message": row[5],
        "archived": bool(row[6]),
        "created_at": _serialize_dt(row[7]),
    }


# ──────────────────────────────────────────────
# PreprocessProgress CRUD
# ──────────────────────────────────────────────

def reset_preprocess_progress(project_id: str, conn_str: str | None = None) -> dict[str, Any]:
    """重置预处理进度（重新触发前清除旧的 complete/error 状态）"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO preprocess_progress
                    (project_id, phase, phase_progress, message, error, completed_at,
                     scan_stats, index_stats, embed_stats, analysis_stats, started_at)
                VALUES (%s, 'idle', 0.0, 'Preprocessing queued...', NULL, NULL,
                        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, NOW())
                ON CONFLICT (project_id) DO UPDATE SET
                    phase = 'idle',
                    phase_progress = 0.0,
                    message = 'Preprocessing queued...',
                    error = NULL,
                    completed_at = NULL,
                    scan_stats = '{}'::jsonb,
                    index_stats = '{}'::jsonb,
                    embed_stats = '{}'::jsonb,
                    analysis_stats = '{}'::jsonb,
                    started_at = NOW()
                RETURNING project_id, phase, phase_progress, message,
                          started_at, completed_at, error,
                          scan_stats, index_stats, embed_stats, analysis_stats
                """,
                (project_id,),
            )
            row = cur.fetchone()
    return _row_to_progress(row)


def upsert_progress(
    project_id: str,
    *,
    phase: str | None = None,
    phase_progress: float | None = None,
    message: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    error: str | None = None,
    scan_stats: dict[str, Any] | None = None,
    index_stats: dict[str, Any] | None = None,
    embed_stats: dict[str, Any] | None = None,
    analysis_stats: dict[str, Any] | None = None,
    conn_str: str | None = None,
) -> dict[str, Any]:
    """插入或更新预处理进度"""
    # 构建动态 upsert — 只更新非 None 字段
    build_sets: list[str] = []
    params: list[Any] = []

    if phase is not None:
        build_sets.append("phase = %s")
        params.append(phase)
    if phase_progress is not None:
        build_sets.append("phase_progress = %s")
        params.append(phase_progress)
    if message is not None:
        build_sets.append("message = %s")
        params.append(message)
    if started_at is not None:
        build_sets.append("started_at = %s")
        params.append(started_at)
    if completed_at is not None:
        build_sets.append("completed_at = %s")
        params.append(completed_at)
    if error is not None:
        build_sets.append("error = %s")
        params.append(error)
    if scan_stats is not None:
        build_sets.append("scan_stats = %s")
        params.append(Jsonb(scan_stats))
    if index_stats is not None:
        build_sets.append("index_stats = %s")
        params.append(Jsonb(index_stats))
    if embed_stats is not None:
        build_sets.append("embed_stats = %s")
        params.append(Jsonb(embed_stats))
    if analysis_stats is not None:
        build_sets.append("analysis_stats = %s")
        params.append(Jsonb(analysis_stats))

    set_clause = ", ".join(build_sets) if build_sets else "phase = phase"

    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO preprocess_progress (project_id)
                VALUES (%s)
                ON CONFLICT (project_id) DO UPDATE SET {set_clause}
                RETURNING project_id, phase, phase_progress, message,
                          started_at, completed_at, error,
                          scan_stats, index_stats, embed_stats, analysis_stats
                """,
                [project_id] + params,
            )
            row = cur.fetchone()
    return _row_to_progress(row)


def get_progress(project_id: str, conn_str: str | None = None) -> dict[str, Any] | None:
    """查询预处理进度"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT project_id, phase, phase_progress, message,
                       started_at, completed_at, error,
                       scan_stats, index_stats, embed_stats, analysis_stats
                FROM preprocess_progress WHERE project_id = %s
                """,
                (project_id,),
            )
            row = cur.fetchone()
    return _row_to_progress(row) if row else None


# ──────────────────────────────────────────────
# 行解析辅助
# ──────────────────────────────────────────────

def _row_to_project(row: tuple) -> dict[str, Any]:
    """将 PG 行转为 dict"""
    return {
        "id": row[0],
        "name": row[1],
        "path": row[2],
        "description": row[3],
        "status": row[4],
        "graph_status": row[5],
        "graph_progress": float(row[6]) if row[6] is not None else 0.0,
        "graph_error": row[7],
        "file_count": row[8],
        "symbol_count": row[9],
        "language_breakdown": row[10] if isinstance(row[10], dict) else (json.loads(row[10]) if row[10] else {}),
        "config": row[11] if isinstance(row[11], dict) else (json.loads(row[11]) if row[11] else {}),
        "created_at": row[12],
        "updated_at": row[13],
    }


def _parse_token_usage(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def _row_to_task(row: tuple) -> dict[str, Any]:
    """将 PG 行转为 dict"""
    return {
        "id": row[0],
        "project_id": row[1],
        "description": row[2],
        "status": row[3],
        "complexity": row[4],
        "plan": row[5] if isinstance(row[5], dict) else (json.loads(row[5]) if row[5] else None),
        "subtask_count": row[6],
        "completed_subtasks": row[7],
        "human_decision": row[8],
        "merged_diff": row[9],
        "thread_id": row[10],
        "token_usage": _parse_token_usage(row[11]),
        "duration_seconds": float(row[12]) if row[12] is not None else None,
        "merge_conflicts": _parse_json_list(row[13]),
        "l3_result": _parse_token_usage(row[14]),
        "created_by_user_id": row[15],
        "created_at": row[16],
        "updated_at": row[17],
        "uploaded_files": _parse_json_list(row[18]) if len(row) > 18 else [],
        "auto_confirm_vision": bool(row[19]) if len(row) > 19 else False,
        "pooled": bool(row[20]) if len(row) > 20 else False,
        "ingest_draft": (row[21] or "") if len(row) > 21 else "",
        # round18 P2 三本账：完成/放弃/剩余。remaining 由三者派生（不落库,永非负）,
        # 让 web 进度反映"已完成 X + 放弃 Y + 剩余 Z"而非只有 completed/count 误导卡死。
        "abandoned_subtasks": (row[22] or 0) if len(row) > 22 else 0,
        "remaining_subtasks": max(
            0,
            (row[6] or 0) - (row[7] or 0) - ((row[22] or 0) if len(row) > 22 else 0),
        ),
        # 队列执行 meta（P0-A：leader 重启后从 DB 重建 _pending_meta）。
        "auto_accept": bool(row[23]) if len(row) > 23 else False,
        "queue_priority": (row[24] or "normal") if len(row) > 24 else "normal",
        # 3rd#2：任务级钉扎 base commit（run_task 启动时捕获；resume 读回不重捕获）。
        "base_commit": (row[25] or None) if len(row) > 25 else None,
    }


def _parse_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _milestone_reports_keep() -> int:
    """每个项目保留的 milestone_reports 最大条数（P2 防无界，默认 200）。"""
    try:
        return max(10, int(os.environ.get("SWARM_MILESTONE_REPORTS_KEEP", "200")))
    except ValueError:
        return 200


def save_milestone_report(
    *,
    project_id: str | None,
    phase: str,
    accept_rate: float,
    threshold: float,
    passed: bool,
    report: dict[str, Any],
    conn_str: str | None = None,
) -> dict[str, Any]:
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO milestone_reports (project_id, phase, accept_rate, threshold, passed, report)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, project_id, phase, accept_rate, threshold, passed, report, created_at
                """,
                (project_id, phase, accept_rate, threshold, passed, Jsonb(report)),
            )
            row = cur.fetchone()
            # P2：milestone_reports 旧实现只增不删 → 长寿项目无界膨胀。每次写入后保留该项目
            # 最近 N 条(默认 200，可配 SWARM_MILESTONE_REPORTS_KEEP)，删除更早的。
            if project_id:
                keep = _milestone_reports_keep()
                cur.execute(
                    """
                    DELETE FROM milestone_reports
                     WHERE project_id = %s
                       AND id NOT IN (
                           SELECT id FROM milestone_reports
                            WHERE project_id = %s
                            ORDER BY created_at DESC, id DESC
                            LIMIT %s
                       )
                    """,
                    (project_id, project_id, keep),
                )
    return {
        "id": row[0],
        "project_id": row[1],
        "phase": row[2],
        "accept_rate": float(row[3]),
        "threshold": float(row[4]),
        "passed": row[5],
        "report": row[6] if isinstance(row[6], dict) else {},
        "created_at": row[7],
    }


def get_latest_milestone_reports(
    project_id: str | None = None,
    limit: int = 10,
    conn_str: str | None = None,
    project_ids: "list[str] | None" = None,
) -> list[dict[str, Any]]:
    # #5(a)：project_ids 非 None 时限定成员项目 scope；空列表 → fail-closed 返回空。
    if project_ids is not None and len(project_ids) == 0:
        return []
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            if project_id:
                cur.execute(
                    """
                    SELECT id, project_id, phase, accept_rate, threshold, passed, report, created_at
                    FROM milestone_reports
                    WHERE project_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (project_id, limit),
                )
            elif project_ids is not None:
                cur.execute(
                    """
                    SELECT id, project_id, phase, accept_rate, threshold, passed, report, created_at
                    FROM milestone_reports
                    WHERE project_id = ANY(%s)
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (list(project_ids), limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, project_id, phase, accept_rate, threshold, passed, report, created_at
                    FROM milestone_reports
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "project_id": r[1],
            "phase": r[2],
            "accept_rate": float(r[3]) if r[3] is not None else None,
            "threshold": float(r[4]) if r[4] is not None else None,
            "passed": r[5],
            "report": r[6] if isinstance(r[6], dict) else {},
            "created_at": r[7],
        }
        for r in rows
    ]


def check_task_token_limit(
    task_id: str,
    *,
    description: str = "",
    merged_diff: str = "",
    subtask_results: dict[str, Any] | None = None,
    subtask_count: int | None = None,
    conn_str: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """返回 (within_limit, token_usage_estimate)。

    round27 弹性预算：有效上限 = max_task_tokens + max_task_tokens_per_subtask×subtask_count
    （与墙钟 P1-B 同理，随规划揭示的任务规模放宽；规划前 count 为 None/0 只用 base，
    防规划自身失控）。base=0 维持既有"关闭闸门"语义。"""
    from swarm.config.settings import get_config

    usage = estimate_token_usage(
        description=description,
        merged_diff=merged_diff,
        subtask_results=subtask_results,
    )
    # B2 治本：用【真实累计】(usage_tracker per-task 记账) 与估算取 max——真实值主导，
    # 估算作 floor 兜底（真实记账可能漏 embed/rerank 之外的边角，但主 LLM 用量已入账）。
    est_total = int(usage.get("total") or 0)
    try:
        from swarm.models import usage_tracker
        real_total = usage_tracker.get_task_total_tokens(task_id)
    except Exception:  # noqa: BLE001
        real_total = 0
    total = max(est_total, real_total)
    usage["estimate_total"] = est_total
    usage["real_recorded"] = real_total
    usage["total"] = total
    usage["estimate"] = real_total <= est_total
    _cfg = get_config()
    limit = _cfg.max_task_tokens
    if limit > 0 and subtask_count:
        per = int(getattr(_cfg, "max_task_tokens_per_subtask", 0) or 0)
        if per > 0:
            limit = limit + per * int(subtask_count)
    usage["limit_effective"] = limit
    if limit > 0 and total > limit:
        update_task(
            task_id,
            status="FAILED",
            token_usage={**usage, "limit_exceeded": True, "limit": limit},
            conn_str=conn_str,
        )
        return False, usage
    return True, usage


def _row_to_progress(row: tuple) -> dict[str, Any]:
    """将 PG 行转为 dict"""
    return {
        "project_id": row[0],
        "phase": row[1],
        "phase_progress": float(row[2]) if row[2] is not None else 0.0,
        "message": row[3],
        "started_at": row[4],
        "completed_at": row[5],
        "error": row[6],
        "scan_stats": row[7] if isinstance(row[7], dict) else (json.loads(row[7]) if row[7] else {}),
        "index_stats": row[8] if isinstance(row[8], dict) else (json.loads(row[8]) if row[8] else {}),
        "embed_stats": row[9] if isinstance(row[9], dict) else (json.loads(row[9]) if row[9] else {}),
        "analysis_stats": row[10] if isinstance(row[10], dict) else (json.loads(row[10]) if row[10] else {}),
    }
