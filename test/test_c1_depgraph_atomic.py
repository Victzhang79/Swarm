"""C1：依赖图重建原子性——DELETE 旧边 + INSERT 新边在【同一连接的同一事务】内。

行为测试：mock sync_pool 捕获调用序列，断言 DELETE 与 INSERT 落在同一 conn、且包在
conn.transaction() 事务里（原子，崩溃回滚不留空图）。不断言实现结构。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

import swarm.project.preprocess as pp


@dataclass
class _Edge:
    source_file: str
    target_file: str
    import_type: str = "import"


class _Cur:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._log.append(("execute", sql.strip().split("\n")[0]))

    def executemany(self, sql, rows):
        self._log.append(("executemany", len(list(rows))))


class _Conn:
    def __init__(self, log):
        self._log = log
        self.in_txn = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @contextmanager
    def transaction(self):
        self._log.append(("txn_enter", None))
        self.in_txn = True
        try:
            yield self
        finally:
            self.in_txn = False
            self._log.append(("txn_exit", None))

    def cursor(self):
        return _Cur(self._log)


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return self._conn


def _run_replace(edges):
    log = []
    conn = _Conn(log)
    # sync_pool 在函数内 `from swarm.infra.db import sync_pool` 导入 → patch 源模块
    with patch("swarm.infra.db.sync_pool", lambda: _Pool(conn)):
        pp._replace_dependency_graph("proj-1", edges)
    return log


def test_delete_and_insert_in_one_transaction():
    log = _run_replace([_Edge("a.py", "b.py"), _Edge("a.py", "c.py")])
    kinds = [k for k, _ in log]
    # 事务包裹 + DELETE 在 INSERT 之前，全在一个 conn
    assert kinds == ["txn_enter", "execute", "executemany", "txn_exit"], log
    assert log[1][1].startswith("DELETE FROM kb_dependency_graph")
    assert log[2] == ("executemany", 2)


def test_empty_edges_still_deletes():
    # 空 edges 仍 DELETE（显式清空），但不 INSERT
    log = _run_replace([])
    kinds = [k for k, _ in log]
    assert kinds == ["txn_enter", "execute", "txn_exit"], log
    assert log[1][1].startswith("DELETE FROM kb_dependency_graph")


def test_exception_is_fail_soft():
    # sync_pool 抛错 → fail-soft 不外抛（依赖图重建绝不影响索引成功）
    with patch("swarm.infra.db.sync_pool", side_effect=RuntimeError("db down")):
        pp._replace_dependency_graph("proj-1", [_Edge("a.py", "b.py")])  # 不应抛
