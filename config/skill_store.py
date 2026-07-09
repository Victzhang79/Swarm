"""经验技能存储（落库，系统级可配，跨项目通用）。

内置种子技能随包发布在 skills_library/；用户在系统级 WebUI 编写/导入的技能落这张表。
loader 合并【内置种子 ∪ DB 技能】(DB 同 id 覆盖内置——用户定制优先)。落库=多实例/容器
共享同一套系统级技能,重启不丢。写入前一律过 experience.validation 准入闸。

镜像 sandbox_store 的模式:ensure_tables 幂等建表 + TTL 缓存 + 写时 invalidate。
任何 db 错误读取返回空(loader 回退纯内置种子),不抛——保证经验层健壮 fail-open。
"""

from __future__ import annotations

import json
import logging
import threading
import time

import psycopg

logger = logging.getLogger(__name__)

EXPERIENCE_SKILLS_DDL = """
CREATE TABLE IF NOT EXISTS experience_skills (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    applies_to_stacks TEXT NOT NULL DEFAULT '["*"]',
    applies_to_intents TEXT NOT NULL DEFAULT '["*"]',
    applies_to_phases TEXT NOT NULL DEFAULT '["*"]',
    target TEXT NOT NULL DEFAULT '["worker"]',
    priority INTEGER NOT NULL DEFAULT 50,
    max_chars INTEGER NOT NULL DEFAULT 1200,
    tags TEXT NOT NULL DEFAULT '[]',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    source TEXT NOT NULL DEFAULT 'user',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""

_CACHE_TTL = 30.0
_cache: list[dict] | None = None
_cache_at: float = 0.0
_lock = threading.Lock()


def _conn_str() -> str:
    from swarm.infra.db import pg_conn_str
    return pg_conn_str()


def ensure_tables(conn_str: str | None = None) -> None:
    """建 experience_skills 表（幂等）。由 app on_startup 调用。"""
    conn_str = conn_str or _conn_str()
    from swarm.infra.db import pg_connect_timeout_kwargs

    with psycopg.connect(conn_str, autocommit=True, **pg_connect_timeout_kwargs()) as conn:
        with conn.cursor() as cur:
            cur.execute(EXPERIENCE_SKILLS_DDL)
    logger.info("experience_skills table ensured")


def _get_conn(conn_str: str | None = None):
    from swarm.infra.db import sync_pool
    return sync_pool(conn_str).connection()


def _row_to_dict(r: tuple) -> dict:
    def _arr(s: str, default: list) -> list:
        try:
            v = json.loads(s)
            return v if isinstance(v, list) else default
        except (ValueError, TypeError):
            return default
    return {
        "id": r[0], "title": r[1], "description": r[2], "body": r[3],
        "applies_to_stacks": _arr(r[4], ["*"]),
        "applies_to_intents": _arr(r[5], ["*"]),
        "applies_to_phases": _arr(r[6], ["*"]),
        "target": _arr(r[7], ["worker"]),
        "priority": r[8], "max_chars": r[9], "tags": _arr(r[10], []),
        "enabled": bool(r[11]), "source": r[12],
    }


_COLS = ("id, title, description, body, applies_to_stacks, applies_to_intents, "
         "applies_to_phases, target, priority, max_chars, tags, enabled, source")


def get_all(conn_str: str | None = None) -> list[dict]:
    """返回全部技能行（含 disabled，供 admin 列表）。db 错误 → []（TTL 缓存）。"""
    global _cache, _cache_at
    now = time.monotonic()
    with _lock:
        if _cache is not None and (now - _cache_at) < _CACHE_TTL:
            return [dict(d) for d in _cache]
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_COLS} FROM experience_skills ORDER BY id")
                rows = cur.fetchall()
        result = [_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        # 可见性(复核 SF#1)：DB 读失败 = 整个用户技能层消失,须 WARNING 非 DEBUG。但本函数在拆 prompt
        # 热路径被调 → 失败也写进缓存(TTL 内不再重试/重复告警),防刷屏 + 防打死 DB。
        logger.warning(
            "读取 experience_skills 失败（回退纯内置种子，%ss 内不重试）: %s", _CACHE_TTL, exc)
        with _lock:
            _cache = []
            _cache_at = now
        return []
    with _lock:
        _cache = [dict(d) for d in result]
        _cache_at = now
    return result


def get_enabled_docs(conn_str: str | None = None) -> list:
    """返回 enabled 技能的 SkillDoc 列表，供 loader 合并。db 空/错误 → []。"""
    from swarm.experience.models import SkillDoc

    docs = []
    for row in get_all(conn_str):
        if not row.get("enabled", True):
            continue
        try:
            docs.append(SkillDoc(
                id=row["id"], title=row["title"] or row["id"], body=row["body"],
                target=tuple(row["target"]) or ("worker",),
                applies_to_stacks=tuple(row["applies_to_stacks"]) or ("*",),
                applies_to_intents=tuple(row["applies_to_intents"]) or ("*",),
                applies_to_phases=tuple(row["applies_to_phases"]) or ("*",),
                priority=int(row["priority"]), max_chars=int(row["max_chars"]),
                summary=row.get("description", ""), tags=tuple(row.get("tags", [])),
                source_path=f"db:{row['id']}", imported=False,
            ))
        except Exception as exc:  # noqa: BLE001 — 单条坏行不拖垮整体
            logger.warning("[skills] DB 技能 %s 转 SkillDoc 失败,跳过: %s", row.get("id"), exc)
    return docs


def upsert(skill: dict, conn_str: str | None = None) -> None:
    """插入/更新一条技能（列表字段存 JSON 文本）。并失效缓存。"""
    def _j(key, default):
        v = skill.get(key, default)
        return json.dumps(list(v) if isinstance(v, (list, tuple)) else default, ensure_ascii=False)
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO experience_skills
                  (id, title, description, body, applies_to_stacks, applies_to_intents,
                   applies_to_phases, target, priority, max_chars, tags, enabled, source,
                   updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                  title=EXCLUDED.title, description=EXCLUDED.description, body=EXCLUDED.body,
                  applies_to_stacks=EXCLUDED.applies_to_stacks,
                  applies_to_intents=EXCLUDED.applies_to_intents,
                  applies_to_phases=EXCLUDED.applies_to_phases, target=EXCLUDED.target,
                  priority=EXCLUDED.priority, max_chars=EXCLUDED.max_chars, tags=EXCLUDED.tags,
                  enabled=EXCLUDED.enabled, source=EXCLUDED.source, updated_at=NOW()
                """,
                (
                    str(skill["id"]), skill.get("title", ""), skill.get("description", ""),
                    skill.get("body", ""), _j("applies_to_stacks", ["*"]),
                    _j("applies_to_intents", ["*"]), _j("applies_to_phases", ["*"]),
                    _j("target", ["worker"]), int(skill.get("priority", 50)),
                    int(skill.get("max_chars", 1200)), _j("tags", []),
                    bool(skill.get("enabled", True)), str(skill.get("source", "user")),
                ),
            )
    invalidate_cache()


def delete(skill_id: str, conn_str: str | None = None) -> bool:
    """删除一条技能。返回是否真的删了一行。"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM experience_skills WHERE id = %s", (str(skill_id),))
            deleted = cur.rowcount > 0
    invalidate_cache()
    return deleted


def set_enabled(skill_id: str, enabled: bool, conn_str: str | None = None) -> bool:
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE experience_skills SET enabled=%s, updated_at=NOW() WHERE id=%s",
                (bool(enabled), str(skill_id)),
            )
            updated = cur.rowcount > 0
    invalidate_cache()
    return updated


def invalidate_cache() -> None:
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0
