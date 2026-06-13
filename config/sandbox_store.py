"""沙箱模板配置存储（落库，系统级可配）。

设计目标（用户需求）：
  - 区分【执行镜像】(exec, 2c2g, agent 写代码用) 和【验证镜像】(verify, 4c4g, 带完整环境+依赖缓存)。
  - 配置落库（不写死 .env/代码），系统级菜单 WebUI 可配，保存后 reload 生效。
  - 方案 B：按子任务性质选镜像，一个沙箱跑到底（不分阶段切沙箱）。
    写代码类子任务用 exec(2c2g)，重编译/集成验证类用 verify(4c4g)。

表 sandbox_templates：每语言一行，存 exec_template + verify_template。
读取优先 db，回退 SandboxConfig 的默认值（向后兼容：db 空时行为不变）。
"""

from __future__ import annotations

import logging
import threading
import time

import psycopg

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

SANDBOX_TEMPLATES_DDL = """
CREATE TABLE IF NOT EXISTS sandbox_templates (
    language TEXT PRIMARY KEY,
    exec_template TEXT NOT NULL DEFAULT '',
    verify_template TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""

# 内存缓存（TTL 最终一致；写时立即失效）。配置类数据，30s 可接受。
_CACHE_TTL = 30.0
_cache: dict[str, dict] | None = None
_cache_at: float = 0.0
_lock = threading.Lock()

# 支持的语言（与 SandboxConfig.template_for_language 一致）
LANGUAGES = ("python", "node", "java", "go", "rust")


def _conn_str() -> str:
    return DatabaseConfig().postgres_uri


def ensure_tables(conn_str: str | None = None) -> None:
    """建 sandbox_templates 表（幂等）。由 init_db / app on_startup 调用。"""
    conn_str = conn_str or _conn_str()
    with psycopg.connect(conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(SANDBOX_TEMPLATES_DDL)
    logger.info("sandbox_templates table ensured")


def _get_conn(conn_str: str | None = None):
    from swarm.infra.db import sync_pool

    return sync_pool(conn_str).connection()


def get_all(conn_str: str | None = None) -> dict[str, dict]:
    """返回 {language: {exec_template, verify_template}}（带 TTL 缓存）。

    任何 db 错误返回空 dict（调用方回退 SandboxConfig 默认），不抛——保证配置读取健壮。
    """
    global _cache, _cache_at
    now = time.monotonic()
    with _lock:
        if _cache is not None and (now - _cache_at) < _CACHE_TTL:
            return dict(_cache)
    try:
        with _get_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT language, exec_template, verify_template FROM sandbox_templates"
                )
                rows = cur.fetchall()
        result = {
            r[0]: {"exec_template": r[1] or "", "verify_template": r[2] or ""}
            for r in rows
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 sandbox_templates 失败（回退默认）: %s", exc)
        return {}
    with _lock:
        _cache = dict(result)
        _cache_at = now
    return result


def get_template(language: str, purpose: str = "exec", conn_str: str | None = None) -> str:
    """取某语言某用途(exec/verify)的模板 ID。db 无则返回空串（调用方回退默认）。"""
    lang = (language or "").lower()
    row = get_all(conn_str).get(lang)
    if not row:
        return ""
    key = "verify_template" if purpose == "verify" else "exec_template"
    return row.get(key, "") or ""


def set_templates(
    language: str,
    exec_template: str,
    verify_template: str,
    conn_str: str | None = None,
) -> None:
    """upsert 某语言的 exec+verify 模板，并失效缓存。"""
    lang = (language or "").lower()
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sandbox_templates (language, exec_template, verify_template, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (language) DO UPDATE SET
                    exec_template = EXCLUDED.exec_template,
                    verify_template = EXCLUDED.verify_template,
                    updated_at = NOW()
                """,
                (lang, exec_template or "", verify_template or ""),
            )
    invalidate_cache()


def invalidate_cache() -> None:
    """失效缓存（配置保存 / reload 后调用）。"""
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0
