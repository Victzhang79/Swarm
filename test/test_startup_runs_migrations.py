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


def test_migration_failure_propagates_out_of_startup():
    """★行为级★：run_migrations 抛异常必须冒出 on_startup（fail-fast，绝不带病启动）。

    ★为何不再用源码窗口切片★（29 号文 T-A6）：原实现断「`run_migrations` 调用行与
    **首个** `ensure_tables` 字样之间的切片里没有 `except` 三个字母」。两条绕法都让它绿：
      ① 用 `with contextlib.suppress(Exception):` 包住那一行 —— 异常被吞而源码里**没有**
         `except` 字样；
      ② 把 try 开在 `run_migrations` **之前**、except 收在 `ensure_tables` **之后**
         （外层兜底）—— 切片窗口内同样无 `except`。
    加重：同文件两个集成测试只断「被调用 + 盖章」，**无任何**测试断异常会向上抛
    ⇒ 迁移 fail-fast 此前只有那一道 getsource 守卫（且它还与 `_pg_available`
    collection 期 skip 叠加）。

    ★本条不需要 PG★：`run_migrations` 之前的语句全是日志/校验/sidecar 或 fail-open
    的 try 块，patch 成抛异常后在碰库之前就炸了 ⇒ CI（无库）同样真跑，不会被 skip 掉。
    """
    import importlib

    import swarm.infra.migrations.runner as runner_mod
    from fastapi.testclient import TestClient

    from swarm.api.app import app
    from swarm.project import store as store_mod

    app_mod = importlib.import_module("swarm.api.app")

    def _boom(*_a, **_k):
        raise RuntimeError("MIGRATION_BOOM_PROBE")

    # ★把 run_migrations 之前的两处不可逆副作用摘掉★（hunter F10，已实测）：
    #   ① `store.register_notification_hook(_push_notification)` 会把一个闭包住**已关闭
    #      event loop** 的 hook 追加进模块级 `_notification_hooks`，**永不摘除** ⇒ 后续任何
    #      create_notification + caplog 断"无 WARNING"的测试会被
    #      `_fire_notification_hooks` 的 "notification hook failed" 污染；
    #   ② `_init_sidecar()` → `apply_sandbox_env()` 把 E2B_API_KEY（**来自 .env 的真 key**）、
    #      E2B_API_URL、CUBE_REMOTE_* 写进 os.environ —— 非 SWARM_ 前缀，conftest 的
    #      `_isolate_swarm_env` 不还原。实测泄漏 1 个 hook + 6 个 env 键。
    # 本条只验"迁移失败会向上抛"，不需要真 startup 的其余部分；摘掉后命题不变
    # （异常仍从 run_migrations 冒出），副作用面归零。
    with patch.object(runner_mod, "run_migrations", _boom), \
         patch.object(app_mod, "_init_sidecar", lambda: None), \
         patch.object(store_mod, "register_notification_hook", lambda _f: None):
        with pytest.raises(RuntimeError, match="MIGRATION_BOOM_PROBE"):
            with TestClient(app):
                pass
    print("  ✅ 迁移失败向上抛（fail-fast）")


def test_wiring_migration_is_failfast_not_swallowed():
    """源码侧辅助守卫：调用行与其后建表 try 之间无兜底。

    ★不再是唯一守卫★：真正的 fail-fast 命题由上面
    `test_migration_failure_propagates_out_of_startup` 行为级钉住（那条才有区分力）。
    本条保留为「读代码时的意图提示」，成本近零；它单独**不足以**证明 fail-fast。
    """
    import importlib
    app_mod = importlib.import_module("swarm.api.app")

    src = inspect.getsource(app_mod.on_startup)
    # run_migrations 调用后紧跟的应是日志+建表注释，而非 except 吞异常。
    after = src[src.index("await loop.run_in_executor(None, run_migrations"):]
    head = after[: after.index("ensure_tables")]
    assert "except" not in head, "run_migrations 被 try/except 包裹→非 fail-fast（P0-C 回归）"
    print("  ✅ run_migrations 为 fail-fast（源码侧辅助）")


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
