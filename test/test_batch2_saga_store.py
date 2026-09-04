"""Batch 2：resume saga 的版本化 schema 与 TaskRecord CRUD 存储契约。"""

from __future__ import annotations

import json

import pytest

from swarm.infra.migrations import runner as migrations
from swarm.project import store


def _task_row(resume_saga=None) -> tuple:
    row = [None] * 31
    row[0:4] = ["task-1", "project-1", "desc", "DELIVERING"]
    row[6:8] = [0, 0]
    row[11] = {}
    row[13] = []
    row[14] = {}
    row[18] = []
    row[22] = 0
    row[29] = {}
    row[30] = {} if resume_saga is None else resume_saga
    return tuple(row)


class _Cursor:
    def __init__(self, row=None):
        self.row = row
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [self.row] if self.row is not None else []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Conn:
    def __init__(self, cursor: _Cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _patch_store_conn(monkeypatch, row=None) -> _Cursor:
    cursor = _Cursor(row)
    monkeypatch.setattr(
        store,
        "_get_conn",
        lambda _conn_str=None: _Conn(cursor),
    )
    return cursor


def _jsonb_value(value):
    return getattr(value, "obj", getattr(value, "adapted", None))


def test_conditional_delete_and_audit_share_one_transaction(monkeypatch):
    """审计 INSERT 失败必须从同一事务冒泡，使前序 DELETE 一并回滚。"""
    class Cursor(_Cursor):
        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "INSERT INTO task_audit_log" in sql:
                raise RuntimeError("audit unavailable")

    class Transaction:
        exit_exception = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, *_args):
            type(self).exit_exception = exc_type
            return False

    class Conn(_Conn):
        def transaction(self):
            return Transaction()

    cursor = Cursor(_task_row({}))
    monkeypatch.setattr(store, "_get_conn", lambda _conn_str=None: Conn(cursor))

    with pytest.raises(RuntimeError, match="audit unavailable"):
        store.delete_task(
            "task-1",
            expected_status="DELIVERING",
            expected_updated_at="epoch-ts",
        )

    sql = " | ".join(statement for statement, _params in cursor.executed)
    assert "status = %s" in sql
    assert "updated_at IS NOT DISTINCT FROM %s" in sql
    assert Transaction.exit_exception is RuntimeError


def test_project_deleting_fence_blocks_task_create_and_active_transition(monkeypatch):
    """store 的原子 SQL 才是围栏：路由旧快照不能让 create/execute 穿越 DELETING。"""
    cursor = _patch_store_conn(monkeypatch, ("DELETING",))
    with pytest.raises(store.ProjectDeletionInProgressError):
        store.create_task("new-task", "project-1", "desc", status="SUBMITTED")
    create_sql = cursor.executed[0][0]
    assert create_sql == "SELECT status FROM projects WHERE id = %s FOR SHARE"
    assert len(cursor.executed) == 1

    cursor = _patch_store_conn(monkeypatch, _task_row({}))
    store.update_task(
        "task-1",
        status="SUBMITTED",
        allow_terminal_transition=True,
        expected_status="FAILED",
    )
    update_sql = cursor.executed[0][0]
    assert "p.id = task_records.project_id" in update_sql
    assert "p.status IS DISTINCT FROM 'DELETING'" in update_sql

    cursor = _patch_store_conn(monkeypatch, _task_row({}))
    store.claim_human_gate("task-1", {"POOLED"}, "SUBMITTED")
    claim_sql = cursor.executed[0][0]
    assert "p.id = task_records.project_id" in claim_sql
    assert "p.status IS DISTINCT FROM 'DELETING'" in claim_sql


def test_project_delete_guard_miss_performs_no_cascade_delete(monkeypatch):
    """最终 DELETING guard 未命中时，关联表必须一行都不删。"""
    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Conn(_Conn):
        def transaction(self):
            return Transaction()

    cursor = _Cursor(("READY",))
    monkeypatch.setattr(store, "_get_conn", lambda _conn_str=None: Conn(cursor))
    monkeypatch.setattr(store, "list_tasks", lambda *_a, **_kw: [])

    assert store.delete_project("project-1", require_deleting=True) is False
    statements = [sql for sql, _params in cursor.executed]
    assert statements == ["SELECT status FROM projects WHERE id = %s FOR UPDATE"]


def test_preprocess_claim_cannot_reopen_deleting_project(monkeypatch):
    """晚到预处理 claim 必须把 DELETING 作为不可穿越终态。"""
    cursor = _patch_store_conn(monkeypatch, None)

    assert store.claim_preprocess_slot("project-1", stale_after_sec=60) is False
    assert len(cursor.executed) == 1
    sql = cursor.executed[0][0]
    assert "status IS DISTINCT FROM 'DELETING'" in sql


def test_cancelled_preprocess_settlement_cannot_revive_deleting_or_new_epoch(monkeypatch):
    """取消结算只可消费当前 PREPROCESSING；DELETING/其他终态必须 CAS miss。"""
    cursor = _patch_store_conn(monkeypatch, None)

    assert store.settle_cancelled_preprocess("project-1") is False
    sql, params = cursor.executed[0]
    assert "status = 'ERROR'" in sql
    assert "status = 'PREPROCESSING'" in sql
    assert params == ("project-1",)
    assert len(cursor.executed) == 1, "项目 CAS miss 时 progress 也必须零写入"

    cursor = _patch_store_conn(monkeypatch, ("project-1",))
    assert store.settle_cancelled_preprocess("project-1") is True
    assert len(cursor.executed) == 2
    progress_sql, progress_params = cursor.executed[1]
    assert "INSERT INTO preprocess_progress" in progress_sql
    assert "phase = 'error'" in progress_sql
    assert "completed_at = NOW()" in progress_sql
    assert progress_params == ("project-1",)


def test_resume_saga_uses_append_only_v10_migration_not_v9_ledger():
    version, name, migrate = migrations._MIGRATIONS[-1]
    assert (version, name) == (10, "task_resume_saga")
    assert all(column != "resume_saga" for _, column, _ in migrations._V9_INLINE_COLUMNS)

    cursor = _Cursor()
    migrate(_Conn(cursor))
    sql = " | ".join(statement for statement, _ in cursor.executed).lower()
    assert "alter table task_records add column if not exists resume_saga" in sql
    assert "jsonb" in sql and "default '{}'" in sql


def test_fresh_task_schema_declares_non_null_empty_resume_saga_default():
    ddl = " ".join(store.TASK_RECORDS_DDL.split()).lower()
    assert "resume_saga jsonb not null default '{}'::jsonb" in ddl


def test_full_row_parser_returns_dict_and_json_text_resume_saga():
    saga = {
        "phase": "claimed",
        "patch_sha256": "abc123",
        "decision": "accept",
        "revert_status": "DELIVERING",
        "updated_at": "2026-09-04T00:00:00Z",
        "detail": {"attempt": 1},
    }
    assert store._row_to_task(_task_row(saga))["resume_saga"] == saga
    assert store._row_to_task(_task_row(json.dumps(saga)))["resume_saga"] == saga
    assert store._row_to_task(_task_row(None))["resume_saga"] == {}


def test_create_task_can_atomically_seed_empty_or_nonempty_resume_saga(monkeypatch):
    saga = {"phase": "claimed", "decision": "revise"}
    cursor = _patch_store_conn(monkeypatch, _task_row(saga))
    monkeypatch.setattr(store, "append_task_audit", lambda *_args, **_kwargs: None)

    task = store.create_task(
        "task-1",
        "project-1",
        "desc",
        resume_saga=saga,
    )

    sql, params = cursor.executed[-1]
    assert cursor.executed[0][0] == "SELECT status FROM projects WHERE id = %s FOR SHARE"
    assert "resume_saga" in sql
    assert any(_jsonb_value(param) == saga for param in params)
    assert task["resume_saga"] == saga


def test_update_task_replaces_resume_saga_for_empty_and_nonempty_values(monkeypatch):
    saga = {"phase": "patch_applied", "patch_sha256": "deadbeef"}
    cursor = _patch_store_conn(monkeypatch, _task_row(saga))

    for value in (saga, {}):
        cursor.executed.clear()
        store.update_task("task-1", resume_saga=value)
        assert len(cursor.executed) == 1
        sql, params = cursor.executed[0]
        assert "resume_saga = %s" in sql
        assert _jsonb_value(params[0]) == value


def test_retry_update_uses_status_and_thread_compare_and_swap(monkeypatch):
    cursor = _patch_store_conn(monkeypatch, _task_row({"kind": "retry_claim"}))

    store.update_task(
        "task-1",
        status="SUBMITTED",
        allow_terminal_transition=True,
        expected_status="FAILED",
        expected_thread_id="old-thread",
        resume_saga={"kind": "retry_claim", "saga_id": "epoch"},
    )

    sql, params = cursor.executed[0]
    assert "status = %s" in sql.split("WHERE", 1)[1]
    assert "COALESCE(thread_id, '') = %s" in sql
    assert params[-2:] == ["FAILED", "old-thread"]


def test_human_gate_claim_writes_recovery_saga_in_same_update(monkeypatch):
    saga = {
        "version": 1,
        "kind": "human_gate_claim",
        "phase": "claimed",
        "revert_status": "DELIVERING",
    }
    cursor = _patch_store_conn(monkeypatch, _task_row(saga))

    task = store.claim_human_gate(
        "task-1",
        {"DELIVERING"},
        "ANALYZING",
        human_decision="ACCEPT",
        resume_saga=saga,
        auto_accept=True,
        queue_priority="urgent",
        expected_resume_saga={},
    )

    sql, params = cursor.executed[0]
    assert "human_decision = %s" in sql
    assert "resume_saga = %s" in sql
    assert "auto_accept = %s" in sql
    assert "queue_priority = %s" in sql
    assert "resume_saga = %s" in sql.split("WHERE", 1)[1]
    assert _jsonb_value(params[2]) == saga
    assert task["resume_saga"] == saga


def test_claim_can_atomically_store_cancellation_account(monkeypatch):
    cursor = _patch_store_conn(monkeypatch, _task_row({}))

    store.claim_human_gate(
        "task-1",
        {"ANALYZING"},
        "CANCELLED",
        resume_saga={},
        token_usage={"cancel_origin": "api_cancel"},
        expected_saga_id="epoch",
    )

    sql, params = cursor.executed[0]
    assert "token_usage = %s" in sql
    assert _jsonb_value(params[2]) == {"cancel_origin": "api_cancel"}
    assert params[-1] == "epoch"


def test_orphan_candidates_include_historical_terminal_unresolved_sagas(monkeypatch):
    cursor = _patch_store_conn(monkeypatch, _task_row({"kind": "execute_claim"}))

    store.list_orphan_candidates()

    sql, params = cursor.executed[0]
    assert "resume_saga->>'kind'" in sql
    assert "resume_saga->>'phase'" in sql
    assert "execute_claim" in params[1]
    assert "recovered_not_applied" in params[2]


def test_get_and_full_list_return_saga_but_light_list_excludes_it(monkeypatch):
    saga = {"phase": "rollback_pending", "revert_status": "DESIGN_REVIEW"}
    cursor = _patch_store_conn(monkeypatch, _task_row(saga))

    assert store.get_task("task-1")["resume_saga"] == saga
    assert store.list_tasks("project-1")[0]["resume_saga"] == saga
    assert all("resume_saga" in sql for sql, _ in cursor.executed)

    assert "resume_saga" not in store._TASK_SELECT_LIGHT
    assert "resume_saga" not in store._row_to_task_light(tuple([None] * 21))
