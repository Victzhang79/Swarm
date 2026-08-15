#!/usr/bin/env python3
"""P2-A 回归：大表 TTL 裁剪 + delete_project 级联补齐。"""

from __future__ import annotations

import contextlib
import inspect


# ── 假 PG（行为锁用：记录全部执行 SQL，绝不触真库）──
class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self._conn = conn
        self._last = None
        self.rowcount = 1

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(str(sql).split()), params))
        self._last = ("public.t",) if "to_regclass" in str(sql).lower() else None

    def fetchone(self):
        return self._last

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.executed: list = []

    def cursor(self):
        return _FakeCursor(self)

    @contextlib.contextmanager
    def transaction(self):
        yield

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def connection(self):
        return self._conn


def _patch_store_pool(monkeypatch, conn: _FakeConn):
    monkeypatch.setattr("swarm.infra.db.sync_pool", lambda *a, **k: _FakePool(conn))


def test_delete_project_cascades_missed_tables(monkeypatch):
    """批25 GS-5w 换锁：原命题=源码断言（三表级联存在 + "DELETE FROM task_audit_log"
    缺席 + task_audit_log 须出现在注释锚点）。
    换成行为锁：真调 delete_project（假连接）——执行 SQL 集必须含三张补齐表
    （milestone_reports/notifications/llm_token_usage）的 DELETE，且绝不含
    task_audit_log 的 DELETE。
    设计意图保留（原注释锚点命题）：task_audit_log 是 append-only 审计，
    delete_project 删项目前已把任务快照写进审计表，审计表本身刻意不被级联删，
    由 TTL purge（purge_old_task_audit）兜底——行为断言成立后注释锚点命题即消失。
    红条件：从级联元组删掉任一补齐表 → 对应断言红；把 task_audit_log 加进级联 → 红。"""
    from swarm.project import store

    conn = _FakeConn()
    _patch_store_pool(monkeypatch, conn)
    assert store.delete_project("p-cascade") is True
    sqls = [s for s, _ in conn.executed]
    for tbl in ("milestone_reports", "notifications", "llm_token_usage"):
        assert f"DELETE FROM {tbl} WHERE project_id = %s" in sqls, \
            f"delete_project 未级联 {tbl}（P2-A 回归，孤立行膨胀）"
    assert not any(s.upper().startswith("DELETE FROM TASK_AUDIT_LOG") for s in sqls), \
        "审计表刻意保留（append-only 追溯删了什么），绝不得级联删"


def test_purge_old_task_audit_disabled_when_nonpositive():
    from swarm.project import store

    assert store.purge_old_task_audit(0) == 0
    assert store.purge_old_task_audit(-5) == 0


def test_purge_old_task_audit_sql_shape(monkeypatch):
    """批25 GS-5w 换锁：原命题=源码字面量（DELETE FROM task_audit_log/make_interval/%s）。
    换成行为锁：真调 purge_old_task_audit（假连接）断参数化 DELETE 真落地——
    保留天数必须走参数（绝不进 SQL 文本），防注入、可反复跑。
    红条件：改成 f-string 拼接天数 → "30" 出现在 SQL 文本 → 红；删掉 DELETE → 红。"""
    from swarm.project import store

    conn = _FakeConn()
    _patch_store_pool(monkeypatch, conn)
    deleted = store.purge_old_task_audit(30)
    audit_deletes = [(s, p) for s, p in conn.executed
                     if s.startswith("DELETE FROM task_audit_log")]
    assert audit_deletes, "裁剪必须真执行 task_audit_log 的 DELETE"
    sql, params = audit_deletes[0]
    assert "make_interval" in sql and "%s" in sql
    assert "30" not in sql, "保留天数绝不能拼进 SQL 文本（注入面）"
    assert params == (30,)
    assert deleted == 1  # 假游标 rowcount=1 → 真返回删除行数


def test_purge_wired_into_daily_scheduler():
    import sys
    import swarm.api.app  # noqa: F401  确保模块已加载
    appmod = sys.modules["swarm.api.app"]  # 绕过 __init__ 把 app(FastAPI 实例)同名遮蔽子模块

    src = inspect.getsource(appmod._run_kb_prune_once)
    assert "purge_old_task_audit" in src, "审计裁剪未接入每日调度（P2-A 回归）"
    assert "SWARM_AUDIT_RETENTION_DAYS" in src


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
