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
#
# ★规范（P0-C 订立）：任何【改列型 / 加约束 / 加索引 / 数据回填】必须作为新版本条目
# 追加到本登记册（version, name, callable），经 run_migrations 应用（on_startup 首启即跑）。
# 禁止再在 ensure_tables 里 inline `ADD COLUMN IF NOT EXISTS` —— 那对已存在列静默跳过，
# 造成「代码期望新型、库仍旧型」的 schema 漂移，且不写 schema_version 无版本可追。
def _migration_v2_task_queue_meta(conn) -> None:
    """v2（P0-A）：task_records 补队列执行 meta 两列，供 leader 重启后从 DB 重建。

    既有库：ADD COLUMN IF NOT EXISTS（幂等）。新库由 TASK_RECORDS_DDL 的 CREATE TABLE 直接建，
    此迁移对其为 no-op。走 versioned runner 而非 store 的 inline _TASK_RECORDS_MIGRATIONS，
    以盖章 schema_version、可追溯（P0-C 规范）。

    用 run_migrations 传入的【同一连接】跑 DDL：① 落在目标库（conn_str 指向哪就哪，不再
    错跑默认库）；② 与 _stamp 同事务，要么全成要么回滚（对账复核 P0-A F1 治本）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS auto_accept BOOLEAN DEFAULT FALSE"
        )
        cur.execute(
            "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS queue_priority TEXT DEFAULT 'normal'"
        )


def _migration_v3_base_commit(conn) -> None:
    """v3（3rd#2）：task_records 加 base_commit，钉住任务启动时的 git HEAD。

    交付链读侧统一相对此 SHA，消除运行期 HEAD 漂移导致的混基线。既有库幂等 ADD COLUMN；
    新库由 TASK_RECORDS_DDL 直建，此迁移 no-op。与 _stamp 同事务（同 P0-A F1 约定）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE task_records ADD COLUMN IF NOT EXISTS base_commit TEXT"
        )


def _migration_v4_token_hash(conn) -> None:
    """v4（F1，round28 安全 P1）：swarm_users.api_token 明文 → SHA256 at-rest 哈希。

    根因：明文存库 + `WHERE api_token=%s` 查——DB 转储即全体长期凭据泄露。治本：加 token_hash
    列（UNIQUE 查找键，只对非空建【部分唯一索引】以容忍回填期/新铸的多个 NULL），把既有明文
    回填为 SHA256 并【清空 api_token 明文】。既有 token 不失效——中间层 get_user_by_token 改为
    先 hash 再查，回填后原明文经同一 hash 仍命中。回填在 Python 侧算 SHA256（不依赖 pgcrypto）。
    与 _stamp 同事务（runner 保证），全成或回滚。
    """
    from swarm.auth.passwords import hash_token

    with conn.cursor() as cur:
        cur.execute("ALTER TABLE swarm_users ADD COLUMN IF NOT EXISTS token_hash TEXT")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_swarm_users_token_hash "
            "ON swarm_users(token_hash) WHERE token_hash IS NOT NULL"
        )
        # 回填既有明文行：token_hash 为空且 api_token 非空。
        cur.execute(
            "SELECT id, api_token FROM swarm_users "
            "WHERE api_token IS NOT NULL AND token_hash IS NULL"
        )
        rows = cur.fetchall()
        for uid, plaintext in rows:
            cur.execute(
                "UPDATE swarm_users SET token_hash=%s, api_token=NULL WHERE id=%s",
                (hash_token(plaintext), uid),
            )
    if rows:
        logger.info("[migrations] v4：回填 %d 个明文 token 为 SHA256 并清空明文", len(rows))


def _migration_v5_kb_file_index_last_modified(conn) -> None:
    """v5：kb_file_index 补 last_modified 列（旧库缺列 → 预处理 upsert 报
    `column "last_modified" does not exist`，文件索引静默存不进，实测 2026-07-06）。

    根因：structure_index.py 的 CREATE TABLE 早已声明 last_modified，但 `CREATE TABLE
    IF NOT EXISTS` 对【已存在的旧表】不补新列——升级前建的库缺这列。既有库幂等 ADD COLUMN；
    新库由 STRUCTURE_INDEX_DDL 直建，此迁移 no-op。DEFAULT now() 与建表 DDL 一致。
    """
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE kb_file_index ADD COLUMN IF NOT EXISTS "
            "last_modified TIMESTAMPTZ DEFAULT now()"
        )


_MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline", _apply_baseline_ddl),
    (2, "add_task_queue_meta", _migration_v2_task_queue_meta),
    (3, "add_base_commit", _migration_v3_base_commit),
    (4, "hash_api_tokens", _migration_v4_token_hash),
    (5, "kb_file_index_last_modified", _migration_v5_kb_file_index_last_modified),
    # 未来迁移在此追加，例如:
    # (5, "add_xxx_column", _migration_add_xxx_column),
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
                # 全新库 → 先跑基线 DDL，再单独盖章。
                # 注意：DDL 与盖章【不在同一事务】（_apply_baseline_ddl 自带连接/事务，
                # 此处 transaction 仅包住 _stamp）。靠 DDL 幂等(IF NOT EXISTS)+盖章幂等保证
                # 中途崩溃后重跑可自愈，而非原子性。
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
            # 常规迁移接收 run_migrations 的连接，DDL 与盖章同事务（原子）。
            # 注：baseline(_apply_baseline_ddl) 走上方 current==0 分支直接调、不经本循环
            # （它需多条独立连接建 pgvector/auth 等，无法共用单连接），故签名差异安全。
            with conn.transaction():
                fn(conn)  # type: ignore[operator]
                with conn.cursor() as cur:
                    _stamp(cur, version, name)
            current = version
