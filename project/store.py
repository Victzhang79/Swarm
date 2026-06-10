"""PostgreSQL 持久化 — Project / TaskRecord / PreprocessProgress CRUD

使用 psycopg 同步模式（与 memory/store.py 一致的模式），
预处理管道中通过 asyncio.to_thread 包装调用。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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
    human_decision TEXT,
    merged_diff TEXT,
    thread_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_records_project ON task_records(project_id);
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

ALL_DDL = [PROJECTS_DDL, TASK_RECORDS_DDL, PREPROCESS_PROGRESS_DDL, MILESTONE_REPORTS_DDL]

# 幂等列迁移（已有库 ADD COLUMN IF NOT EXISTS）
_TASK_RECORDS_MIGRATIONS = [
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS token_usage JSONB DEFAULT '{}'",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS duration_seconds REAL",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS merge_conflicts JSONB DEFAULT '[]'",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS l3_result JSONB DEFAULT '{}'",
    "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS created_by_user_id TEXT",
]

_TASK_SELECT = """
    id, project_id, description, status, complexity,
    plan, subtask_count, completed_subtasks,
    human_decision, merged_diff, thread_id,
    token_usage, duration_seconds,
    merge_conflicts, l3_result, created_by_user_id,
    created_at, updated_at
"""


# ──────────────────────────────────────────────
# 连接辅助
# ──────────────────────────────────────────────

def _get_conn_str(db_config: DatabaseConfig | None = None) -> str:
    """获取 PG 连接字符串"""
    cfg = db_config or DatabaseConfig()
    return cfg.postgres_uri


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
                    updated_at = NOW()
                RETURNING id, name, path, description, status, graph_status,
                          graph_progress, graph_error, file_count, symbol_count,
                          language_breakdown, config, created_at, updated_at
                """,
                (project_id, name, path, description, Jsonb(config or {})),
            )
            row = cur.fetchone()
    return _row_to_project(row)


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
        sets.append("config = %s")
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


def delete_project(project_id: str, conn_str: str | None = None) -> bool:
    """删除项目及其关联数据（task_records + preprocess_progress 级联删除需手动）"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            # 先删关联
            cur.execute("DELETE FROM task_records WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM preprocess_progress WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM projects WHERE id = %s", (project_id,))
            deleted = cur.rowcount
    return deleted > 0


# ──────────────────────────────────────────────
# TaskRecord CRUD
# ──────────────────────────────────────────────

def create_task(
    task_id: str,
    project_id: str,
    description: str,
    created_by_user_id: str | None = None,
    conn_str: str | None = None,
) -> dict[str, Any]:
    """创建任务记录"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_records (id, project_id, description, created_by_user_id)
                VALUES (%s, %s, %s, %s)
                RETURNING id, project_id, description, status, complexity,
                          plan, subtask_count, completed_subtasks,
                          human_decision, merged_diff, thread_id,
                          token_usage, duration_seconds,
                          merge_conflicts, l3_result, created_by_user_id,
                          created_at, updated_at
                """,
                (task_id, project_id, description, created_by_user_id),
            )
            row = cur.fetchone()
    return _row_to_task(row)


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


def update_task(
    task_id: str,
    *,
    status: str | None = None,
    complexity: str | None = None,
    plan: dict[str, Any] | None = None,
    subtask_count: int | None = None,
    completed_subtasks: int | None = None,
    human_decision: str | None = None,
    merged_diff: str | None = None,
    thread_id: str | None = None,
    token_usage: dict[str, Any] | None = None,
    duration_seconds: float | None = None,
    merge_conflicts: list[dict[str, Any]] | None = None,
    l3_result: dict[str, Any] | None = None,
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
                RETURNING id, project_id, description, status, complexity,
                          plan, subtask_count, completed_subtasks,
                          human_decision, merged_diff, thread_id,
                          token_usage, duration_seconds,
                          created_at, updated_at
                """,
                params,
            )
            row = cur.fetchone()
    return _row_to_task(row) if row else None


def delete_task(task_id: str, conn_str: str | None = None) -> bool:
    """删除任务"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM task_records WHERE id = %s", (task_id,))
            deleted = cur.rowcount
    return deleted > 0


# ──────────────────────────────────────────────
# Task stats & notifications (Phase 5)
# ──────────────────────────────────────────────

_TERMINAL_STATUSES = ("DONE", "FAILED", "CANCELLED")
_NOTIFY_STATUSES = ("DONE", "FAILED", "CONFIRMING", "DELIVERING")


def _task_event_type(status: str) -> str:
    if status == "DONE":
        return "task_completed"
    if status == "FAILED":
        return "task_failed"
    if status in ("CONFIRMING", "DELIVERING"):
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
    """对比 mem_mistakes 近 30 天 vs 前 30 天数量，判断学习趋势"""
    default: dict[str, Any] = {
        "recent_mistakes": 0,
        "prior_mistakes": 0,
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
    except Exception as exc:
        logger.debug("learning_effectiveness unavailable: %s", exc)
        return default

    if recent == 0 and prior == 0:
        trend = "unknown"
    elif recent < prior:
        trend = "improving"
    else:
        trend = "stable"

    return {"recent_mistakes": recent, "prior_mistakes": prior, "trend": trend}


def get_task_stats(project_id: str | None = None, conn_str: str | None = None) -> dict[str, Any]:
    """聚合任务统计；可选按 project_id 过滤"""
    where = "WHERE project_id = %s" if project_id else ""
    params: tuple[Any, ...] = (project_id,) if project_id else ()

    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_tasks,
                    COUNT(*) FILTER (WHERE status = 'DONE') AS completed,
                    COUNT(*) FILTER (WHERE status = 'FAILED') AS failed,
                    COUNT(*) FILTER (WHERE status = 'CANCELLED') AS cancelled,
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
            total, completed, failed, cancelled, approved = counts

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
        "approved": approved,
        "accept_rate": accept_rate,
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
    if status in ("CONFIRMING", "DELIVERING"):
        return f"待审核: {short}"
    return short


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
) -> list[dict[str, Any]]:
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
    conn_str: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """返回 (within_limit, token_usage_estimate)。"""
    from swarm.config.settings import get_config

    usage = estimate_token_usage(
        description=description,
        merged_diff=merged_diff,
        subtask_results=subtask_results,
    )
    limit = get_config().max_task_tokens
    total = int(usage.get("total") or 0)
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
