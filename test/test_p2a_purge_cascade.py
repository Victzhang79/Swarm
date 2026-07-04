#!/usr/bin/env python3
"""P2-A 回归：大表 TTL 裁剪 + delete_project 级联补齐。"""

from __future__ import annotations

import inspect


def test_delete_project_cascades_missed_tables():
    """delete_project 级联补齐 milestone_reports/notifications/llm_token_usage（防孤立行膨胀）。"""
    from swarm.project import store

    src = inspect.getsource(store.delete_project)
    for tbl in ("milestone_reports", "notifications", "llm_token_usage"):
        assert tbl in src, f"delete_project 未级联 {tbl}（P2-A 回归）"
    # task_audit_log 故意保留（追溯），不得被级联删
    assert "DELETE FROM task_audit_log" not in src
    assert "task_audit_log" in src  # 注释里说明为何保留


def test_purge_old_task_audit_disabled_when_nonpositive():
    from swarm.project import store

    assert store.purge_old_task_audit(0) == 0
    assert store.purge_old_task_audit(-5) == 0


def test_purge_old_task_audit_sql_shape():
    """裁剪 SQL 用参数化 interval，按 at 删旧行（不注入、可反复跑）。"""
    from swarm.project import store

    src = inspect.getsource(store.purge_old_task_audit)
    assert "DELETE FROM task_audit_log" in src
    assert "make_interval" in src and "at <" in src
    assert "%s" in src  # 参数化，非拼接


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
