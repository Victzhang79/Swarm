"""W2.2 回归测试 — 轻量迁移运行器 + delete_project 原子性。

迁移用假连接/假游标模拟（不依赖真 PG）：
- 既有库：to_regclass 非 NULL + schema_version 空 → 只盖章 baseline，不跑基线 DDL。
- 全新库：to_regclass NULL + schema_version 空 → 跑基线 DDL 后盖章。
- 幂等：再跑一次不重复应用。

delete_project：源码静态断言级联删除包在 conn.transaction() 内（A-P1-23）。
"""

from __future__ import annotations

import contextlib
import inspect
from unittest.mock import patch

import swarm.infra.migrations.runner as runner


# ───────────────────────── 假 PG ─────────────────────────
class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self._conn = conn
        self._last_result = None

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        s = sql.lower()
        if "max(version)" in s:
            self._last_result = (self._conn.max_version,)
        elif "to_regclass" in s:
            self._last_result = (self._conn.sentinel,)
        elif "insert into schema_version" in s:
            # 记录盖章 + 更新已应用版本
            ver = params[0] if params else None
            self._conn.stamped.append(ver)
            if ver is not None:
                self._conn.max_version = max(self._conn.max_version, ver)
            self._last_result = None
        else:
            self._last_result = None

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        # v4(F1) 回填读 swarm_users 明文行——fake 空库无行可回填，返回空列表。
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sentinel, max_version=0):
        self.sentinel = sentinel          # to_regclass 返回值（既有库=表名，全新库=None）
        self.max_version = max_version    # schema_version MAX(version)
        self.executed: list = []
        self.stamped: list = []
        self.tx_depth = 0

    def cursor(self):
        return _FakeCursor(self)

    @contextlib.contextmanager
    def transaction(self):
        self.tx_depth += 1
        yield
        self.tx_depth -= 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def connection(self):
        return self._conn


@contextlib.contextmanager
def _patch_pool(conn: _FakeConn):
    with patch("swarm.infra.db.sync_pool", return_value=_FakePool(conn)):
        yield


# ───────────────────────── 既有库：盖章不跑 DDL ─────────────────────────
def test_existing_db_stamps_baseline_without_running_ddl():
    conn = _FakeConn(sentinel="public.projects", max_version=0)
    with _patch_pool(conn):
        with patch.object(runner, "_apply_baseline_ddl") as ddl:
            runner.run_migrations("postgresql://x")
            ddl.assert_not_called()  # 既有库绝不重跑基线 DDL
    assert 1 in conn.stamped, "既有库应盖章 baseline(version=1)"


# ───────────────────────── 全新库：跑 DDL 后盖章 ─────────────────────────
def test_fresh_db_runs_baseline_then_stamps():
    conn = _FakeConn(sentinel=None, max_version=0)
    with _patch_pool(conn):
        with patch.object(runner, "_apply_baseline_ddl") as ddl:
            runner.run_migrations("postgresql://x")
            ddl.assert_called_once()  # 全新库必须跑基线 DDL
    assert 1 in conn.stamped, "全新库跑完 DDL 后应盖章 baseline(version=1)"


# ───────────────────────── 幂等：已应用不重复 ─────────────────────────
def test_idempotent_already_applied_does_nothing():
    # schema_version 已到最新版（当前 = v2 add_task_queue_meta）→ 不再决策基线、不再盖章。
    _latest = runner._MIGRATIONS[-1][0]
    conn = _FakeConn(sentinel="public.projects", max_version=_latest)
    with _patch_pool(conn):
        with patch.object(runner, "_apply_baseline_ddl") as ddl:
            runner.run_migrations("postgresql://x")
            ddl.assert_not_called()
    assert conn.stamped == [], "已应用全部迁移时不应再盖章"


def test_running_twice_does_not_reapply():
    """连续两次：第一次盖章 baseline(1)+后续迁移，第二次因 schema_version 已到最新而短路。"""
    all_versions = [v for v, _, _ in runner._MIGRATIONS]  # 当前 = [1, 2]
    conn = _FakeConn(sentinel="public.projects", max_version=0)
    with _patch_pool(conn):
        with patch.object(runner, "_apply_baseline_ddl") as ddl:
            runner.run_migrations("postgresql://x")  # 第一次：盖章全部
            stamped_after_first = list(conn.stamped)
            runner.run_migrations("postgresql://x")  # 第二次：max_version 已到最新，短路
            ddl.assert_not_called()  # 既有库基线只盖章不跑 DDL
    assert stamped_after_first == all_versions
    assert conn.stamped == all_versions, "第二次运行不应再盖章（幂等）"


def test_schema_version_table_ensured():
    conn = _FakeConn(sentinel="public.projects", max_version=1)
    with _patch_pool(conn):
        runner.run_migrations("postgresql://x")
    sqls = " | ".join(s for s, _ in conn.executed)
    assert "create table if not exists schema_version" in sqls.lower()


# ─────────────── v5：kb_file_index 补 last_modified 列 ───────────────
def test_v5_adds_kb_file_index_last_modified():
    """既有旧库（缺 last_modified 列）跑迁移应 ADD COLUMN IF NOT EXISTS，幂等补列。

    根因（实测 2026-07-06）：CREATE TABLE IF NOT EXISTS 不给已存在旧表补新列 → 预处理
    upsert 报 `column "last_modified" does not exist`，文件索引静默存不进。
    """
    conn = _FakeConn(sentinel="public.projects", max_version=4)  # 停在 v4，只差 v5
    with _patch_pool(conn):
        with patch.object(runner, "_apply_baseline_ddl"):
            runner.run_migrations("postgresql://x")
    sqls = " | ".join(s for s, _ in conn.executed).lower()
    assert "alter table kb_file_index add column if not exists last_modified" in sqls, \
        "v5 迁移必须给 kb_file_index 幂等补 last_modified 列"
    assert 5 in conn.stamped, "v5 迁移应被盖章"


# ───────────────────────── delete_project 原子性 ─────────────────────────
def test_delete_project_uses_transaction():
    """A-P1-23：级联删除必须包在 conn.transaction() 内（autocommit 池连接下才原子）。"""
    from swarm.project import store

    src = inspect.getsource(store.delete_project)
    assert "conn.transaction()" in src, \
        "delete_project 级联删除必须用 conn.transaction() 保证原子"
    # transaction 块必须真的包住 DELETE projects（在 with 之后出现）
    tx_idx = src.index("conn.transaction()")
    del_idx = src.index("DELETE FROM projects")
    assert tx_idx < del_idx, "DELETE projects 必须在 transaction 块内"


if __name__ == "__main__":
    import sys

    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n=== migrations: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
