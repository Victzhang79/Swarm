#!/usr/bin/env python3
"""统一建表入口 — 单一事实来源。

所有表的 DDL 都定义在各业务模块内（project/store.py、memory/store.py、
knowledge/*、auth/store.py）。本脚本汇总调用它们的 ensure_tables，
**不再重复 DDL**，从根本上杜绝 setup.sh 与代码 schema 漂移。

setup.sh 在 PG 就绪后调用本脚本完成建表；应用启动钩子（api/app.py:on_startup）
也会调用相同的 ensure_tables，二者完全一致、幂等。

用法:
    python scripts/init_db.py            # 用 .env / 默认连接串建全部表
    SWARM_DB_POSTGRES_URI=... python scripts/init_db.py
"""

from __future__ import annotations

import sys


def _bootstrap_swarm_package() -> None:
    """确保 `import swarm` 指向项目根（与 test/swarm_bootstrap.py 一致）。"""
    try:
        import swarm  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    import types
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pkg = types.ModuleType("swarm")
    pkg.__path__ = [str(root)]
    sys.modules["swarm"] = pkg


_bootstrap_swarm_package()

from swarm.config.settings import DatabaseConfig  # noqa: E402


def _ensure_pgvector(conn_str: str) -> None:
    """启用 pgvector 扩展（记忆/知识库向量列依赖）。"""
    import psycopg

    with psycopg.connect(conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    print("  ✅ pgvector 扩展已启用")


def _ensure_sync_tables() -> None:
    """同步建表：project/task/preprocess/milestone。

    注意：auth 表单独放在 _ensure_auth_tables_after_memory()，因为 auth 的
    _PROFILE_MIGRATION 会 ALTER mem_user_profile，必须在 memory 表建好之后才能跑。
    """
    from swarm.models.capability_store import ensure_tables as ensure_capability_tables
    from swarm.project.store import ensure_tables as ensure_project_tables

    ensure_project_tables()
    print("  ✅ project / task_records / preprocess_progress / milestone_reports")
    ensure_capability_tables()
    print("  ✅ model_capabilities")
    from swarm.config.secret_store import ensure_tables as ensure_secret_tables

    ensure_secret_tables()
    print("  ✅ secret_store（敏感信息加密存储）")
    from swarm.config.sandbox_store import ensure_tables as ensure_sandbox_tpl_tables

    ensure_sandbox_tpl_tables()
    print("  ✅ sandbox_templates（沙箱执行/验证镜像配置）")
    from swarm.config.command_blacklist_store import ensure_tables as ensure_cmd_blacklist_tables

    ensure_cmd_blacklist_tables()
    print("  ✅ command_blacklist（命令安全黑名单 + 内置默认规则）")


def _ensure_auth_tables() -> None:
    """建 auth/RBAC 表。必须在 memory 表（mem_user_profile）建好之后调用，
    因为 ensure_auth_tables 内含 ALTER TABLE mem_user_profile 迁移 + bootstrap
    admin 会写 mem_user_profile（全新空库若先跑 auth 会报 relation 不存在）。"""
    from swarm.auth.store import ensure_auth_tables

    ensure_auth_tables()
    print("  ✅ auth / RBAC 表")


async def _ensure_async_tables() -> None:
    """异步建表：memory L1-L6 + knowledge Layer A/C/D。"""
    from swarm.knowledge.behavior_store import BehaviorStore
    from swarm.knowledge.norms_store import NormsStore
    from swarm.knowledge.structure_index import StructureIndexer
    from swarm.memory.store import MemoryStore

    db = DatabaseConfig()

    mem = MemoryStore(db)
    await mem.connect()  # connect() 内部自动 ensure_tables()
    await mem.close()
    print("  ✅ memory: mem_user_profile / mem_task_summary / mem_mistakes / mem_successes")

    struct = StructureIndexer(db)
    await struct.connect()
    await struct.close()
    print("  ✅ knowledge Layer A: kb_file_index / kb_symbol_index / kb_dependency_graph")

    norms = NormsStore(db)
    await norms.connect()
    await norms.close()
    print("  ✅ knowledge Layer C: kb_norms")

    behavior = BehaviorStore(db)
    await behavior.connect()
    await behavior.close()
    print("  ✅ knowledge Layer D: kb_modification_log / kb_co_occurrence / kb_mr_history")

    # kb_update_events（增量更新队列）— DDL 常量在 updater 模块，
    # 直接执行以避免 KnowledgeUpdater.connect() 拉起 Qdrant 依赖。
    import psycopg
    from swarm.knowledge.updater import EVENT_QUEUE_DDL

    async with await psycopg.AsyncConnection.connect(
        db.postgres_uri, autocommit=True
    ) as conn:
        async with conn.cursor() as cur:
            await cur.execute(EVENT_QUEUE_DDL)
    print("  ✅ knowledge 增量队列: kb_update_events")

    # A1 批1：LangGraph PG checkpointer 表（多副本共享 + 跨副本 interrupt/resume）。
    # langgraph 自带幂等 setup()，并入统一入口让全新库一次建好（与 startup 一致）。
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(db.postgres_uri) as cp:
            await cp.setup()
        print("  ✅ LangGraph checkpointer: checkpoints / checkpoint_writes / checkpoint_blobs")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️  checkpointer 表建表跳过（不阻断，startup 会重试）: {exc}")


def main() -> int:
    db = DatabaseConfig()
    conn_str = db.postgres_uri
    # 脱敏打印
    shown = conn_str
    if "@" in shown:
        shown = shown.split("@", 1)[0].rsplit(":", 1)[0] + ":***@" + shown.split("@", 1)[1]
    print(f"🗄️  初始化数据库: {shown}")

    try:
        # W2.2：统一走轻量迁移运行器。它内部用 to_regclass 探针决策——
        # 既有库只「盖章」基线不重跑 DDL，全新库才按本模块的确切顺序跑基线 DDL
        # （memory 先于 auth）。helper 函数(_ensure_*)仍是单一事实来源，被 runner 复用。
        from swarm.infra.migrations import run_migrations

        run_migrations(conn_str)
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 建表失败: {exc}", file=sys.stderr)
        return 1

    print("\n✅ 全部数据表就绪（schema 由各业务模块统一定义，迁移由 schema_version 追踪）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
