#!/usr/bin/env python3
"""P0-C：on_startup 必须先跑 run_migrations 再 ensure_tables。

修前：run_migrations 只在 scripts/init_db.py 调；容器/直起走 on_startup 从不调 →
schema_version 永不 stamp、版本化迁移形同虚设、将来 ALTER 不自动应用 → schema 漂移。
修后：on_startup 在第一个 ensure_tables 之前调 run_migrations（fail-fast）。

- test_wiring_*：源码级装配守卫，无需 PG，CI 安全（防有人删掉调用/调换顺序）。
- test_integration_*：真实启动，_pg_available 守卫（CI 无库则跳过）。
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


# ── 装配守卫（无 PG） ─────────────────────────────────


def test_wiring_on_startup_calls_migrations_before_ensure_tables():
    """on_startup 源码里 run_migrations 调用必须出现在首个 ensure_tables 之前。"""
    import importlib
    app_mod = importlib.import_module("swarm.api.app")

    src = inspect.getsource(app_mod.on_startup)
    assert "run_migrations" in src, "on_startup 未调用 run_migrations（P0-C 回归）"
    # 调用形式为 run_in_executor(None, run_migrations, None)——搜 executor 调用而非 name()。
    idx_mig = src.index("run_in_executor(None, run_migrations")
    idx_ensure = src.index("store.ensure_tables")  # 首个真实建表调用（非注释字样）
    assert idx_mig < idx_ensure, "run_migrations 必须在 ensure_tables 之前调用"
    print("  ✅ on_startup 先 run_migrations 后 ensure_tables")


def test_wiring_migration_is_failfast_not_swallowed():
    """迁移调用不得被 try/except 吞（fail-fast）。校验调用行与其后建表 try 之间无兜底。"""
    import importlib
    app_mod = importlib.import_module("swarm.api.app")

    src = inspect.getsource(app_mod.on_startup)
    # run_migrations 调用后紧跟的应是日志+建表注释，而非 except 吞异常。
    after = src[src.index("await loop.run_in_executor(None, run_migrations"):]
    head = after[: after.index("ensure_tables")]
    assert "except" not in head, "run_migrations 被 try/except 包裹→非 fail-fast（P0-C 回归）"
    print("  ✅ run_migrations 为 fail-fast")


# ── 集成（需 PG） ────────────────────────────────────


@pytest.mark.skipif(not _pg_available(), reason="PG 不可达")
def test_integration_startup_invokes_run_migrations_and_stamps():
    """真实启动：run_migrations 被调用一次，且 schema_version 存在 baseline 行。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app
    import swarm.infra.migrations.runner as runner_mod

    real = runner_mod.run_migrations
    calls: list[str] = []

    def _spy(conn_str=None):
        calls.append("migrate")
        return real(conn_str)

    # 只 spy 迁移，其余 startup 走真实路径（已被现有 auth 测试证明可跑通）。
    with patch.object(runner_mod, "run_migrations", _spy):
        with TestClient(app):
            pass

    assert calls == ["migrate"], f"run_migrations 未被 startup 调用一次: {calls}"

    # schema_version 有 baseline 行（版本 >= 1）。
    with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
            maxv = cur.fetchone()[0]
    assert maxv >= 1, f"schema_version 未 stamp baseline: max={maxv}"
    print("  ✅ 真实启动跑迁移 + stamp schema_version")


@pytest.mark.skipif(not _pg_available(), reason="PG 不可达")
def test_integration_v2_adds_task_queue_meta_columns():
    """P0-A v2 迁移：run_migrations 后 task_records 必须有 auto_accept + queue_priority 列，
    且 schema_version 盖章到 v2。"""
    from swarm.infra.migrations.runner import run_migrations

    run_migrations(None)  # 幂等

    with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'task_records'
                  AND column_name IN ('auto_accept', 'queue_priority')
                """
            )
            cols = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
            maxv = cur.fetchone()[0]
    assert cols == {"auto_accept", "queue_priority"}, f"v2 列缺失: {cols}"
    assert maxv >= 2, f"schema_version 未盖章 v2: max={maxv}"
    print("  ✅ v2 迁移补齐队列 meta 列 + 盖章 v2")


if __name__ == "__main__":
    test_wiring_on_startup_calls_migrations_before_ensure_tables()
    test_wiring_migration_is_failfast_not_swallowed()
    if _pg_available():
        test_integration_startup_invokes_run_migrations_and_stamps()
        test_integration_v2_adds_task_queue_meta_columns()
    else:
        print("  ⏭ PG 不可达，跳过集成")
    print("\n✅ P0-C/P0-A 全过")
