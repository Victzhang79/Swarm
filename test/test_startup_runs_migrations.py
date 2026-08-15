#!/usr/bin/env python3
"""P0-C：on_startup 必须先跑 run_migrations 再 ensure_tables。

修前：run_migrations 只在 scripts/init_db.py 调；容器/直起走 on_startup 从不调 →
schema_version 永不 stamp、版本化迁移形同虚设、将来 ALTER 不自动应用 → schema 漂移。
修后：on_startup 在第一个 ensure_tables 之前调 run_migrations（fail-fast）。

- test_startup_calls_migrations_before_ensure_tables：★批25 GS-5w 换锁★行为级顺序守卫
  （真跑 on_startup + spy 两调用记序），下游副作用全 patch，无需 PG，CI 安全
  （防有人删掉调用/调换顺序——原 getsource 序断言已被本锁替代）。
- test_integration_*：真实启动，`needs_service("pg")` 标记守卫（#29-4 T-7：判定在
  runtest setup 期，非 collection 期；CI 上声明起了 postgres service，连不上=硬失败）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ★#29-4 T-7★ 原有 `_pg_available()` 被用在**装饰器实参**里（`@skipif(not
# _pg_available(), …)`）⇒ collection 期求值一次。PG 抖一下这两条集成测试就降级 skip，
# 而它们是"迁移失败必须 fail-fast"的**唯一真跑守护**——skip 掉之后只剩静态断言。
# 改用 `needs_service("pg")` 标记：判定推迟到 runtest setup，CI 上（明确起了 postgres
# service）连不上直接硬失败。实现见 test/conftest.py::pytest_runtest_setup。


# ── 装配守卫（无 PG） ─────────────────────────────────


def test_startup_calls_migrations_before_ensure_tables():
    """★批25 GS-5w 换锁★（原命题：on_startup 源码里 run_migrations 调用必须出现在首个
    ensure_tables 之前——getsource 序断言；现改为行为锁）：真跑 on_startup，spy
    run_migrations 与 store.ensure_tables 记录实际调用序（P0-C）。

    区分力：删掉 run_migrations 调用 → calls 缺 "migrate" → 红；把它挪到 ensure_tables
    之后 → 序翻转 → 红。下游副作用面全部 patch 成 no-op（无需 PG，CI 安全）——
    本锁只守「先迁移后建表」这一件事，其余启动步骤不是被测对象。"""
    import asyncio
    import importlib
    from unittest.mock import AsyncMock, MagicMock

    import swarm.auth.store as auth_store_mod
    import swarm.brain.graph as graph_mod
    import swarm.brain.runner as brain_runner_mod
    import swarm.config.command_blacklist_store as command_blacklist_mod
    import swarm.config.sandbox_store as sandbox_store_mod
    import swarm.config.secret_store as secret_store_mod
    import swarm.config.skill_store as skill_store_mod
    import swarm.infra.migrations.runner as runner_mod
    import swarm.infra.scheduler_leadership as leadership_mod
    import swarm.models.capability_store as capability_store_mod

    app_mod = importlib.import_module("swarm.api.app")
    from swarm.project import store as store_mod

    calls: list[str] = []

    def _spy_migrate(conn_str=None):
        calls.append("migrate")

    def _spy_ensure():
        calls.append("ensure")

    def _noop(*_a, **_k):
        return None

    def _close_bg(coro):
        coro.close()  # 不起后台调度（它们会摸 DB/Redis），只闭环协程防 never-awaited

    # 摘掉迁移【前后】的全部不可逆副作用/外部依赖（同
    # test_migration_failure_propagates_out_of_startup 的 hunter F10 教训：notification
    # hook 与 sidecar env 泄漏会毒化别的测试），保证本锁在无 PG 环境同样真跑。
    with patch.object(runner_mod, "run_migrations", _spy_migrate), \
         patch.object(store_mod, "ensure_tables", _spy_ensure), \
         patch.object(store_mod, "register_notification_hook", _noop), \
         patch.object(app_mod, "_init_sidecar", _noop), \
         patch.object(app_mod, "_spawn_bg", _close_bg), \
         patch.object(app_mod, "_sweep_startup_orphans", _noop), \
         patch.object(app_mod, "_start_sandbox_pool_reaper", _noop), \
         patch.object(capability_store_mod, "ensure_tables", _noop), \
         patch.object(secret_store_mod, "ensure_tables", _noop), \
         patch.object(sandbox_store_mod, "ensure_tables", _noop), \
         patch.object(command_blacklist_mod, "ensure_tables", _noop), \
         patch.object(skill_store_mod, "ensure_tables", _noop), \
         patch.object(auth_store_mod, "ensure_auth_tables", _noop), \
         patch.object(auth_store_mod, "ensure_bootstrap_admin",
                      lambda **_: MagicMock(id="u-admin")), \
         patch.object(auth_store_mod, "ensure_admin_default_profile", _noop), \
         patch.object(auth_store_mod, "backfill_legacy_project_members", _noop), \
         patch.object(graph_mod, "init_postgres_checkpointer",
                      AsyncMock(return_value=False)), \
         patch.object(leadership_mod, "init_coordination_backend", AsyncMock()), \
         patch.object(brain_runner_mod, "reconcile_orphan_tasks", AsyncMock()):
        asyncio.run(app_mod.on_startup())

    assert calls == ["migrate", "ensure"], (
        f"on_startup 必须先 run_migrations 再 store.ensure_tables（P0-C 回归），"
        f"实际调用序: {calls}")
    print("  ✅ on_startup 先 run_migrations 后 ensure_tables（行为级）")


def test_migration_failure_propagates_out_of_startup():
    """★行为级★：run_migrations 抛异常必须冒出 on_startup（fail-fast，绝不带病启动）。

    ★为何不再用源码窗口切片★（29 号文 T-A6）：原实现断「`run_migrations` 调用行与
    **首个** `ensure_tables` 字样之间的切片里没有 `except` 三个字母」。两条绕法都让它绿：
      ① 用 `with contextlib.suppress(Exception):` 包住那一行 —— 异常被吞而源码里**没有**
         `except` 字样；
      ② 把 try 开在 `run_migrations` **之前**、except 收在 `ensure_tables` **之后**
         （外层兜底）—— 切片窗口内同样无 `except`。
    加重：同文件两个集成测试只断「被调用 + 盖章」，**无任何**测试断异常会向上抛
    ⇒ 迁移 fail-fast 此前只有那一道 getsource 守卫（且它还与 collection 期 skip
    叠加——那一层已由 #29-4 T-7 的 `needs_service` 标记治掉）。

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


# ★批25 GS-5w 换锁★：原 `test_wiring_migration_is_failfast_not_swallowed`（getsource
# 窗口切片断言「迁移调用与建表之间无 except」）已删除——同文件
# `test_migration_failure_propagates_out_of_startup` 行为锁已钉住「迁移失败冒出
# on_startup（fail-fast 不吞）」这一真命题，且区分力更强（窗口切片防不住
# contextlib.suppress / 外层兜底两种吞法，见该测试 docstring 的 29 号文 T-A6 分析）。
# 勿再补源码切片守卫：断「字面量」是纪律 6 明禁的焊死测试形态。


# ── 集成（需 PG） ────────────────────────────────────


@pytest.mark.needs_service("pg")
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


@pytest.mark.needs_service("pg")
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


def _pg_reachable_for_main() -> bool:
    """★仅供下面 `__main__` 直跑用★（`python test/test_xxx.py` 这条老路径）。

    #29-4 T-7 把 pytest 侧的判定改成了 `needs_service` 标记（runtest setup 期求值），
    但 `__main__` 分支不经 pytest，没有标记机制，故保留一个**本地**探测。
    刻意不叫 `_pg_available`：那个名字曾被用在装饰器实参里，留着同名函数会让下一个人
    很容易再写回 `@skipif(not _pg_available(), …)` —— 换名字是为了让老范式不再顺手。
    """
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


if __name__ == "__main__":
    test_startup_calls_migrations_before_ensure_tables()
    test_migration_failure_propagates_out_of_startup()
    if _pg_reachable_for_main():
        test_integration_startup_invokes_run_migrations_and_stamps()
        test_integration_v2_adds_task_queue_meta_columns()
    else:
        print("  ⏭ PG 不可达，跳过集成")
    print("\n✅ P0-C/P0-A 全过")
