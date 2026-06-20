"""轻量级自研迁移运行器。

设计（W2.2 决定的基线策略：探针盖章，不重跑 DDL）：

1. 确保 schema_version 表存在 (version INT PK, name TEXT, applied_at TIMESTAMPTZ)。
2. 读当前已应用最大版本（空表=0）。
3. 若 schema_version 为空：用 to_regclass('public.projects') 探针判断是不是既有库。
   - 非 NULL（既有库，表早就建好）→ 只 INSERT 基线行(version=1, name='baseline')
     【盖章】，绝不重跑基线 DDL（重跑既无必要又有风险）。
   - NULL（全新库）→ 按 scripts/init_db.py 的【确切顺序】跑基线 DDL（memory 必须先于
     auth），然后盖章 version=1。
4. 应用所有 version > 当前 的迁移（升序）。基线之后暂无迁移；未来新增迁移只需往
   _MIGRATIONS 追加 (version, name, callable)。

每个迁移的「应用 + 写 schema_version」包在一个事务里，要么全成要么回滚。
连接走 infra.db 的池化连接（autocommit），用 conn.transaction() 显式开事务。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INT PRIMARY KEY,
    name       TEXT,
    applied_at TIMESTAMPTZ DEFAULT now()
)
"""

# 既有库探针：projects 表是 init_db 建的第一张业务表，存在即视为「库已建好」。
_BASELINE_SENTINEL = "public.projects"


def _apply_baseline_ddl() -> None:
    """跑基线 DDL —— 严格复刻 scripts/init_db.py main() 的调用顺序。

    顺序至关重要：memory 表(mem_user_profile)必须先于 auth，因为 auth 的
    _PROFILE_MIGRATION 会 ALTER mem_user_profile、bootstrap admin 也会写它。
    """
    import asyncio

    # init_db 把这些函数当作单一事实来源；这里直接复用，避免 DDL 二次漂移。
    from swarm.scripts.init_db import (
        _ensure_async_tables,
        _ensure_auth_tables,
        _ensure_pgvector,
        _ensure_sync_tables,
    )
    from swarm.config.settings import DatabaseConfig

    conn_str = DatabaseConfig().postgres_uri
    _ensure_pgvector(conn_str)
    _ensure_sync_tables()
    asyncio.run(_ensure_async_tables())
    _ensure_auth_tables()


# ── 迁移登记册（升序，append-only）──────────────────
# 每项: (version, name, callable | None)。callable=None 表示「纯盖章」基线那种
# 特殊情形在 run_migrations 内单独处理；常规迁移都带 callable。
_MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline", _apply_baseline_ddl),
    # 未来迁移在此追加，例如:
    # (2, "add_xxx_column", _migration_add_xxx_column),
]

_BASELINE_VERSION = 1


def _max_applied_version(cur) -> int:
    cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _sentinel_exists(cur) -> bool:
    """to_regclass 探针：既有库返回非 NULL。"""
    cur.execute("SELECT to_regclass(%s)", (_BASELINE_SENTINEL,))
    row = cur.fetchone()
    return bool(row and row[0] is not None)


def _stamp(cur, version: int, name: str) -> None:
    cur.execute(
        "INSERT INTO schema_version (version, name) VALUES (%s, %s) "
        "ON CONFLICT (version) DO NOTHING",
        (version, name),
    )


def run_migrations(conn_str: str | None = None) -> None:
    """迁移单一入口。幂等：重复运行不会重复应用任何迁移。

    Args:
        conn_str: 目标库连接串；None 用默认（.env / DatabaseConfig）。
    """
    from swarm.infra.db import sync_pool

    with sync_pool(conn_str).connection() as conn:
        # 1. 确保 schema_version 表（DDL 幂等，独立提交即可）
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_VERSION_DDL)

        # 2. 读当前最大版本
        with conn.cursor() as cur:
            current = _max_applied_version(cur)

        # 3. 基线：仅当 schema_version 为空时决策盖章 vs 跑 DDL
        if current == 0:
            with conn.cursor() as cur:
                existing_db = _sentinel_exists(cur)
            if existing_db:
                # 既有库 → 只盖章，不重跑基线 DDL
                logger.info(
                    "[migrations] 既有库(projects 已存在) → 盖章 baseline v%d，不重跑 DDL",
                    _BASELINE_VERSION,
                )
                with conn.transaction():
                    with conn.cursor() as cur:
                        _stamp(cur, _BASELINE_VERSION, "baseline")
            else:
                # 全新库 → 跑基线 DDL 后盖章（同一事务保证原子）
                logger.info("[migrations] 全新库 → 运行 baseline DDL 后盖章 v%d", _BASELINE_VERSION)
                _apply_baseline_ddl()
                with conn.transaction():
                    with conn.cursor() as cur:
                        _stamp(cur, _BASELINE_VERSION, "baseline")
            current = _BASELINE_VERSION

        # 4. 应用 version > current 的常规迁移（升序）
        for version, name, fn in _MIGRATIONS:
            if version <= current:
                continue
            if fn is None:
                continue
            logger.info("[migrations] 应用迁移 v%d (%s)", version, name)
            with conn.transaction():
                fn()  # type: ignore[operator]
                with conn.cursor() as cur:
                    _stamp(cur, version, name)
            current = version
