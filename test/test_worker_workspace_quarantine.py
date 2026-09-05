"""Worker 工作区隔离持久化与 marker CRUD 回归。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _quarantine_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_INSTANCE_STATE_DIR", str(tmp_path / "instance-state"))


def test_malformed_persistent_quarantine_fails_closed(monkeypatch):
    from swarm.worker import workspace_quarantine

    marker = workspace_quarantine._marker_path("project-1")
    marker.parent.mkdir(parents=True)
    marker.write_text("not-json")
    monkeypatch.setattr(
        "swarm.project.store.get_worker_workspace_quarantine",
        lambda _project_id: None,
    )

    record = workspace_quarantine._load_sync("project-1")

    assert record is not None
    assert record["errors"][0].startswith("worker_quarantine_local_invalid")


def test_quarantine_marker_write_handles_short_write_and_fsyncs_directory(monkeypatch):
    from swarm.worker import workspace_quarantine

    real_write = os.write
    real_fsync = os.fsync
    write_calls = []
    fsync_calls = []

    def _short_first_write(fd, payload):
        write_calls.append(len(payload))
        return real_write(fd, payload[:1] if len(write_calls) == 1 else payload)

    def _record_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "write", _short_first_write)
    monkeypatch.setattr(os, "fsync", _record_fsync)
    record = {"active": True, "token": "t", "errors": ["x"]}

    workspace_quarantine._write_local(record, "project-short-write")

    assert len(write_calls) >= 2
    assert len(fsync_calls) == 2, "文件内容与 rename 后的目录元数据都要落盘"
    assert workspace_quarantine._read_local("project-short-write")["token"] == "t"


def test_quarantine_marker_replace_failure_removes_temporary_file(monkeypatch):
    from swarm.worker import workspace_quarantine

    marker = workspace_quarantine._marker_path("project-replace-fail")
    monkeypatch.setattr(
        os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        workspace_quarantine._write_local(
            {"active": True, "token": "t"}, "project-replace-fail"
        )

    assert not marker.exists()
    assert list(marker.parent.glob(".*.tmp")) == []


def test_quarantine_clear_holds_project_lock_and_uses_token_cas(monkeypatch):
    from swarm.worker import workspace_quarantine

    events = []

    class _Lock:
        def __init__(self, project_id, key):
            events.append(("lock", project_id, key))

        def acquire(self):
            events.append("acquire")
            return True

        def release(self):
            events.append("release")

    async def _load(_project_id, _path):
        return {
            "token": "current-token",
            "errors": ["fixed"],
            "_sources": ["db"],
        }

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", _Lock)
    monkeypatch.setattr(workspace_quarantine, "load_workspace_quarantine", _load)
    monkeypatch.setattr(
        "swarm.project.store.clear_worker_workspace_quarantine",
        lambda project_id, token: events.append(("cas", project_id, token)) or True,
    )

    cleared = asyncio.run(workspace_quarantine.clear_workspace_quarantine(
        "project-1", "/workspace/project", "current-token"
    ))

    assert cleared is True
    assert events == [
        ("lock", "project-1", "default"),
        "acquire",
        ("cas", "project-1", "current-token"),
        "release",
    ]


def test_malformed_local_quarantine_has_recoverable_cas_token(monkeypatch):
    from swarm.worker import workspace_quarantine

    marker = workspace_quarantine._marker_path("project-bad")
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"not-json")
    monkeypatch.setattr(
        "swarm.project.store.get_worker_workspace_quarantine",
        lambda _project_id: None,
    )
    record = workspace_quarantine._load_sync("project-bad")

    assert record and str(record["token"]).startswith("invalid-")
    assert workspace_quarantine._clear_local("project-bad", record["token"])
    assert not marker.exists()


def test_db_and_redis_unavailable_fails_loud_and_keeps_process_fence(
    tmp_path, monkeypatch
):
    from swarm.worker import workspace_quarantine

    monkeypatch.setattr(
        "swarm.project.store.set_worker_workspace_quarantine",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("PG down")),
    )
    monkeypatch.setattr(workspace_quarantine, "_write_redis", lambda *_a: False)
    with pytest.raises(RuntimeError, match="PG 耐久持久化失败"):
        workspace_quarantine.persist_workspace_quarantine_sync(
            "project-local", str(tmp_path), ["restore failed"]
        )
    monkeypatch.setattr(
        "swarm.project.store.get_worker_workspace_quarantine",
        lambda _project_id: None,
    )

    loaded = workspace_quarantine._load_sync("project-local")
    assert loaded and set(loaded["_sources"]) == {"local", "process"}


def test_pg_and_redis_failure_is_visible_to_other_instance_sharing_tree(
    tmp_path, monkeypatch
):
    from swarm.worker import workspace_quarantine

    monkeypatch.setattr(workspace_quarantine, "_write_redis", lambda *_a: False)
    monkeypatch.setattr(workspace_quarantine, "_read_redis", lambda *_a: None)
    monkeypatch.setattr(
        "swarm.project.store.set_worker_workspace_quarantine",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("PG down")),
    )
    monkeypatch.setattr(
        "swarm.project.store.get_worker_workspace_quarantine",
        lambda _project_id: None,
    )

    with pytest.raises(RuntimeError, match="PG 耐久持久化失败"):
        workspace_quarantine.persist_workspace_quarantine_sync(
            "project-shared-tree", str(tmp_path), ["restore failed"]
        )
    workspace_quarantine._PROCESS_FENCES.clear()
    monkeypatch.setenv("SWARM_INSTANCE_STATE_DIR", str(tmp_path / "other-instance"))

    loaded = workspace_quarantine._load_sync("project-shared-tree", str(tmp_path))
    assert loaded and loaded["_sources"] == ["workspace"]


def test_workspace_quarantine_never_follows_project_state_symlink(tmp_path):
    from swarm.worker import workspace_quarantine

    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".swarm-state").symlink_to(outside, target_is_directory=True)
    record = {
        "active": True,
        "token": "safe-token",
        "project_path": str(project),
        "errors": ["x"],
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    workspace_quarantine._write_workspace(record, "project-safe", str(project))

    assert list(outside.rglob("*")) == []
    loaded = workspace_quarantine._read_workspace("project-safe", str(project))
    assert loaded and loaded["token"] == "safe-token"


def test_linked_worktree_workspace_quarantine_does_not_dirty_git_status(tmp_path):
    from swarm.worker import workspace_quarantine

    source = tmp_path / "source"
    linked = tmp_path / "linked"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(source), "add", "base.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "base"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "-q", str(linked)],
        check=True,
    )
    record = {
        "active": True,
        "token": "worktree-token",
        "project_path": str(linked),
        "errors": ["x"],
        "created_at": "2026-01-01T00:00:00+00:00",
    }

    workspace_quarantine._write_workspace(record, "project-worktree", str(linked))

    status = subprocess.run(
        ["git", "-C", str(linked), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert status.stdout == ""


@pytest.mark.parametrize("kind", ["fifo", "oversize"])
def test_workspace_quarantine_invalid_marker_never_blocks_or_reads_unbounded(
    tmp_path, kind
):
    from swarm.worker import workspace_quarantine

    project = tmp_path / "project"
    project.mkdir()
    fd = workspace_quarantine._open_workspace_state_dir(str(project), create=True)
    assert fd is not None
    digest = hashlib.sha256(b"project-bounded").hexdigest()
    name = f"{digest}.invalid.json"
    try:
        if kind == "fifo":
            os.mkfifo(name, dir_fd=fd)
        else:
            marker_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            try:
                os.write(marker_fd, b"x" * (workspace_quarantine._MAX_MARKER_BYTES + 1))
            finally:
                os.close(marker_fd)
    finally:
        os.close(fd)

    started = time.monotonic()
    record = workspace_quarantine._read_workspace("project-bounded", str(project))

    assert time.monotonic() - started < 1
    assert record and record["active"] is True
    assert record["errors"][0].startswith("worker_quarantine_workspace_invalid:")


def test_redis_bridge_is_visible_cross_instance_while_pg_persist_fails(
    tmp_path, monkeypatch
):
    from swarm.worker import workspace_quarantine

    class _Redis:
        def __init__(self):
            self.data = {}

        def hset(self, name, key, value):
            self.data.setdefault(name, {})[key] = value

        def hgetall(self, name):
            return dict(self.data.get(name, {}))

        def hdel(self, name, key):
            self.data.get(name, {}).pop(key, None)

    shared = _Redis()
    monkeypatch.setattr("swarm.infra.redis_client.get_redis", lambda: shared)
    monkeypatch.setattr(
        "swarm.project.store.set_worker_workspace_quarantine",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("PG down")),
    )
    monkeypatch.setattr(
        "swarm.project.store.get_worker_workspace_quarantine",
        lambda _project_id: None,
    )

    with pytest.raises(RuntimeError, match="PG 耐久持久化失败"):
        workspace_quarantine.persist_workspace_quarantine_sync(
            "project-shared", str(tmp_path), ["restore failed"]
        )
    # 模拟另一进程：无本进程 fence，也无本机 marker。
    workspace_quarantine._PROCESS_FENCES.clear()
    monkeypatch.setenv("SWARM_INSTANCE_STATE_DIR", str(tmp_path / "other-instance"))

    loaded = workspace_quarantine._load_sync("project-shared")
    assert loaded
    assert loaded["_sources"] == ["redis"]


def test_enabled_but_unavailable_redis_cannot_prove_quarantine_empty(monkeypatch):
    from swarm.worker import workspace_quarantine

    monkeypatch.setenv("SWARM_REDIS_ENABLED", "true")
    monkeypatch.setattr("swarm.infra.redis_client.get_redis", lambda: None)
    monkeypatch.setattr(
        "swarm.project.store.get_worker_workspace_quarantine",
        lambda _project_id: None,
    )

    with pytest.raises(RuntimeError, match="quarantine_redis_read_failed"):
        workspace_quarantine._load_sync("project-redis-unknown")


def test_parallel_quarantine_incidents_cannot_clear_each_other(tmp_path, monkeypatch):
    from swarm.worker import workspace_quarantine

    incidents = []

    def _set(project_id, project_path, errors, *, token=None, created_at=None):
        record = {
            "active": True,
            "token": token,
            "project_path": str(Path(project_path).resolve()),
            "errors": list(errors),
            "created_at": created_at or f"2026-01-01T00:00:0{len(incidents)}+00:00",
        }
        incidents.append(record)
        return record

    def _get(_project_id):
        if not incidents:
            return None
        return {**incidents[0], "_pending_count": len(incidents)}

    def _clear(_project_id, token):
        for index, record in enumerate(incidents):
            if record["token"] == token:
                incidents.pop(index)
                return True
        return False

    class _Lock:
        def __init__(self, *_args):
            pass

        def acquire(self):
            return True

        def release(self):
            return None

    monkeypatch.setattr(workspace_quarantine, "_write_redis", lambda *_a: False)
    monkeypatch.setattr(workspace_quarantine, "_read_redis", lambda *_a: None)
    monkeypatch.setattr("swarm.project.store.set_worker_workspace_quarantine", _set)
    monkeypatch.setattr("swarm.project.store.get_worker_workspace_quarantine", _get)
    monkeypatch.setattr("swarm.project.store.clear_worker_workspace_quarantine", _clear)
    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", _Lock)

    first = workspace_quarantine.persist_workspace_quarantine_sync(
        "project-parallel", str(tmp_path), ["A remains"]
    )
    second = workspace_quarantine.persist_workspace_quarantine_sync(
        "project-parallel", str(tmp_path), ["B remains"]
    )

    # B 的 token 不是当前最老未解决 incident，不能跨过 A 把围栏清空。
    assert not asyncio.run(workspace_quarantine.clear_workspace_quarantine(
        "project-parallel", str(tmp_path), second["token"]
    ))
    current = workspace_quarantine._load_sync("project-parallel")
    assert current and current["token"] == first["token"]
    assert current["pending_incidents"] == 2

    assert asyncio.run(workspace_quarantine.clear_workspace_quarantine(
        "project-parallel", str(tmp_path), first["token"]
    ))
    remaining = workspace_quarantine._load_sync("project-parallel")
    assert remaining and remaining["token"] == second["token"]


def test_project_store_quarantine_crud_preserves_sibling_incidents(monkeypatch):
    """不替换 CRUD 本体：通过 fake connection 执行生产 SQL 契约。"""
    from swarm.project import store

    rows = []

    class _Cursor:
        def __init__(self):
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            normalized = " ".join(sql.lower().split())
            if normalized.startswith("insert into worker_workspace_quarantine"):
                assert "on conflict (project_id, token)" in normalized
                project_id, token, path, errors, created_at = params
                if not any(r[0] == project_id and r[1] == token for r in rows):
                    rows.append(
                        (project_id, token, path, list(getattr(errors, "obj", errors)), created_at)
                    )
                self.result = (project_id,)
                return
            if normalized.startswith("select token, project_path"):
                assert "count(*) over ()" in normalized
                project_rows = sorted(
                    (r for r in rows if r[0] == params[0]), key=lambda r: (r[4], r[1])
                )
                self.result = (
                    (project_rows[0][1], project_rows[0][2], project_rows[0][3],
                     project_rows[0][4], len(project_rows))
                    if project_rows else None
                )
                return
            if normalized.startswith("delete from worker_workspace_quarantine"):
                assert "project_id = %s and token = %s" in normalized
                project_id, token = params
                index = next(
                    (i for i, row in enumerate(rows) if row[0] == project_id and row[1] == token),
                    None,
                )
                self.result = (rows.pop(index)[0],) if index is not None else None
                return
            raise AssertionError(f"未覆盖 SQL: {normalized}")

        def fetchone(self):
            return self.result

    class _Conn:
        def cursor(self):
            return _Cursor()

    @contextmanager
    def _connection():
        yield _Conn()

    monkeypatch.setattr(store, "_get_conn", _connection)
    store.set_worker_workspace_quarantine(
        "p", "/workspace/p", ["A"], token="token-a",
        created_at="2026-01-01T00:00:00+00:00",
    )
    store.set_worker_workspace_quarantine(
        "p", "/workspace/p", ["B"], token="token-b",
        created_at="2026-01-01T00:00:01+00:00",
    )

    current = store.get_worker_workspace_quarantine("p")
    assert current and current["token"] == "token-a"
    assert current["_pending_count"] == 2
    assert store.clear_worker_workspace_quarantine("p", "token-a") is True
    remaining = store.get_worker_workspace_quarantine("p")
    assert remaining and remaining["token"] == "token-b"
    assert remaining["_pending_count"] == 1


def test_all_persistent_quarantine_writes_fail_still_blocks_same_process(
    tmp_path, monkeypatch
):
    from swarm.worker import workspace_quarantine

    workspace_quarantine._PROCESS_FENCES.clear()
    monkeypatch.setattr(
        workspace_quarantine,
        "_write_local",
        lambda *_a: (_ for _ in ()).throw(OSError("disk readonly")),
    )
    monkeypatch.setattr(
        "swarm.project.store.set_worker_workspace_quarantine",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("PG down")),
    )
    monkeypatch.setattr(workspace_quarantine, "_write_redis", lambda *_a: False)
    with pytest.raises(RuntimeError, match="PG 耐久持久化失败"):
        workspace_quarantine.persist_workspace_quarantine_sync(
            "project-process", str(tmp_path), ["restore failed"]
        )
    monkeypatch.setattr(
        "swarm.project.store.get_worker_workspace_quarantine",
        lambda _project_id: None,
    )
    loaded = workspace_quarantine._load_sync("project-process")
    assert loaded and loaded["_sources"] == ["process"]


def test_quarantine_clear_keeps_db_fence_when_local_cas_fails(monkeypatch):
    from swarm.worker import workspace_quarantine

    effects = []

    class _Lock:
        def __init__(self, *_args):
            pass

        def acquire(self):
            return True

        def release(self):
            effects.append("released")

    async def _load(*_args):
        return {
            "token": "db-token",
            "_local_token": "local-token",
            "_sources": ["local", "db"],
            "errors": ["restore failed"],
        }

    monkeypatch.setattr("swarm.infra.redis_client.ModuleLock", _Lock)
    monkeypatch.setattr(workspace_quarantine, "load_workspace_quarantine", _load)
    monkeypatch.setattr(
        workspace_quarantine,
        "_clear_local",
        lambda *_args: effects.append("local-failed") or False,
    )

    cleared = asyncio.run(workspace_quarantine.clear_workspace_quarantine(
        "project-1", "/workspace/project", "db-token"
    ))

    assert cleared is False
    assert effects == ["local-failed", "released"]
