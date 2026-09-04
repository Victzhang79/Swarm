"""Batch 2：项目删除与知识库维护的持久栅栏行为。"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.knowledge.updater import ChangeType, FileChange, KnowledgeUpdater, UpdateEvent


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class _FenceCursor:
    def __init__(self, project_status: str | None, pending_rows=None):
        self.project_status = project_status
        self.pending_rows = list(pending_rows or [])
        self.executed: list[tuple[str, object]] = []
        self._one = None
        self._all = []

    async def execute(self, sql, params=None):
        self.executed.append((str(sql), params))
        normalized = " ".join(str(sql).split())
        if normalized.startswith("SELECT status FROM projects"):
            self._one = ((self.project_status,) if self.project_status is not None else None)
        elif normalized.startswith("INSERT INTO kb_update_events"):
            self._one = (42,)
        elif "RETURNING id, project_id, payload_json" in normalized:
            self._all = list(self.pending_rows)
        elif "FROM kb_pending_embeddings" in normalized and normalized.startswith("SELECT"):
            self._all = list(self.pending_rows)

    async def fetchone(self):
        return self._one

    async def fetchall(self):
        return self._all


class _FenceConnection:
    def __init__(self, project_status: str | None, pending_rows=None):
        self.cursor_obj = _FenceCursor(project_status, pending_rows)

    def cursor(self):
        return _AsyncContext(self.cursor_obj)

    def transaction(self):
        return _AsyncContext()


@pytest.mark.asyncio
@pytest.mark.parametrize("project_status", ["DELETING", None])
async def test_enqueue_rejects_deleting_or_missing_project_atomically(project_status):
    """旧 repair/webhook 快照不能在删除围栏后重新创建 KB 事件。"""
    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._conn = _FenceConnection(project_status)
    updater._lock = asyncio.Lock()
    event = UpdateEvent(
        project_id="p-deleting",
        changes=[FileChange("src/a.py", ChangeType.MODIFIED, content="x = 1\n")],
        metadata={"source": "consistency_repair"},
    )

    with pytest.raises(RuntimeError, match="知识库更新"):
        await updater.enqueue_event(event)

    statements = [sql for sql, _ in updater._conn.cursor_obj.executed]
    assert not any("INSERT INTO kb_update_events" in sql for sql in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("project_status", ["DELETING", None])
async def test_consumer_discards_event_without_touching_layers_when_project_unavailable(
    project_status,
):
    """已排队事件也必须在真正写 Layer 前重新确认项目仍存在且未删除。"""
    payload = {
        "changes": [
            {
                "file_path": "src/a.py",
                "change_type": "modified",
                "old_path": None,
                "language": "python",
            }
        ],
        "metadata": {},
    }
    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._conn = _FenceConnection(project_status, [(7, "p-deleting", payload)])
    updater._lock = asyncio.Lock()

    async def _must_not_write(_event):
        raise AssertionError("删除中的项目不得进入知识层写入")

    updater.handle_event = _must_not_write

    async def _noop_reconcile():
        return None

    updater._maybe_reconcile_stuck = _noop_reconcile

    assert await updater.process_pending_events(batch_size=10) == 0
    statements = [" ".join(sql.split()) for sql, _ in updater._conn.cursor_obj.executed]
    assert any("project_unavailable" in sql for sql in statements)


@pytest.mark.asyncio
async def test_depgraph_rebuild_does_not_write_after_project_fence_rejects(monkeypatch):
    """后台依赖图旧快照必须在真写前经过同一项目维护栅栏。"""
    from swarm.knowledge import updater as updater_module

    entered: list[str] = []

    @asynccontextmanager
    async def _rejecting_fence(project_id, *, operation):
        entered.append(f"{project_id}:{operation}")
        raise updater_module.ProjectKnowledgeUpdateRejected("project unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(
        updater_module,
        "project_knowledge_write_fence",
        _rejecting_fence,
        raising=False,
    )

    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._lock = asyncio.Lock()
    wrote: list[str] = []

    def _must_not_write(*_args):
        wrote.append("dependency_graph")

    monkeypatch.setattr(
        "swarm.project.preprocess._replace_dependency_graph",
        _must_not_write,
    )

    await updater._rebuild_depgraph_async("p-deleting")

    assert entered and entered[0].startswith("p-deleting:")
    assert wrote == []


@pytest.mark.asyncio
async def test_depgraph_busy_retries_rebuild_signal(monkeypatch):
    """暂时拿不到项目锁不能把已达阈值的重建信号永久吃掉。"""
    from swarm.knowledge import updater as updater_module
    from swarm.knowledge.project_fence import ProjectKnowledgeWriterBusy

    attempts = 0

    @asynccontextmanager
    async def _busy_fence(project_id, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProjectKnowledgeWriterBusy("busy")
        yield {"id": project_id, "status": "READY", "path": "/tmp/project"}

    monkeypatch.setattr(updater_module, "project_knowledge_write_fence", _busy_fence)
    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._lock = asyncio.Lock()
    updater._kb_config = SimpleNamespace(depgraph_rebuild_threshold=3)
    updater._depgraph_dirty = {"p-busy": 0}
    wrote: list[str] = []
    monkeypatch.setattr("swarm.project.codegraph.is_codegraph_installed", lambda: True)
    monkeypatch.setattr(
        "swarm.project.codegraph.run_codegraph_full",
        lambda _path: SimpleNamespace(edges=[SimpleNamespace(source_file="a", target_file="b")]),
    )
    monkeypatch.setattr(
        "swarm.project.preprocess._replace_dependency_graph",
        lambda project_id, _edges: wrote.append(project_id),
    )

    await updater._rebuild_depgraph_async("p-busy")

    assert attempts == 2
    assert wrote == ["p-busy"]


@pytest.mark.asyncio
async def test_project_write_fence_renews_until_body_finishes(monkeypatch):
    """长维护超过初始 TTL 时，第二个 writer 仍不能取得项目宽锁。"""
    from swarm.infra import redis_client
    from swarm.knowledge.project_fence import project_knowledge_write_fence
    from swarm.project import store

    class _ExpiringLock:
        held_until = 0.0
        owner = None

        def __init__(self, project_id, module_key, *, ttl_sec=0.05):
            self.ttl_sec = ttl_sec
            self.token = object()
            self._held = False

        def acquire(self):
            now = time.monotonic()
            if self.__class__.owner is not None and now < self.__class__.held_until:
                return False
            self.__class__.owner = self.token
            self.__class__.held_until = now + self.ttl_sec
            self._held = True
            return True

        def renew(self):
            if self.__class__.owner is not self.token:
                return False
            self.__class__.held_until = time.monotonic() + self.ttl_sec
            return True

        def release(self):
            if self.__class__.owner is self.token:
                self.__class__.owner = None
            self._held = False

    monkeypatch.setattr(redis_client, "ModuleLock", _ExpiringLock)
    monkeypatch.setattr(
        store,
        "get_project",
        lambda project_id: {"id": project_id, "status": "READY"},
    )

    async with project_knowledge_write_fence(
        "p-long",
        operation="test",
        ttl_sec=0.05,
        renew_interval_seconds=0.01,
    ):
        await asyncio.sleep(0.08)
        contender = _ExpiringLock("p-long", "default", ttl_sec=0.05)
        assert contender.acquire() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("renew_mode", ["false", "raises"])
async def test_project_write_fence_drains_owned_write_before_release_on_lease_loss(
    monkeypatch,
    renew_mode,
):
    """续期失败会停写，但必须等已进入线程的副作用收尾后才能释放锁。"""
    from swarm.infra import redis_client
    from swarm.infra.cancellation import run_blocking_owned
    from swarm.knowledge.project_fence import (
        ProjectKnowledgeLeaseLost,
        project_knowledge_write_fence,
    )
    from swarm.project import store

    trace: list[str] = []

    class _LostLock:
        def __init__(self, *_args, ttl_sec=0.05, **_kwargs):
            self.ttl_sec = ttl_sec

        def acquire(self):
            return True

        def renew(self):
            trace.append("renew_lost")
            if renew_mode == "raises":
                raise RuntimeError("renew transport failed")
            return False

        def release(self):
            trace.append("release")

    def _blocking_write():
        time.sleep(0.05)
        trace.append("write_finished")

    monkeypatch.setattr(redis_client, "ModuleLock", _LostLock)
    monkeypatch.setattr(
        store,
        "get_project",
        lambda project_id: {"id": project_id, "status": "READY"},
    )

    with pytest.raises(ProjectKnowledgeLeaseLost):
        async with project_knowledge_write_fence(
            "p-lost",
            operation="test",
            ttl_sec=0.05,
            renew_interval_seconds=0.01,
        ):
            await run_blocking_owned(_blocking_write, operation="test write")

    assert trace.index("renew_lost") < trace.index("write_finished") < trace.index("release")


@pytest.mark.asyncio
async def test_project_write_fence_translates_own_cancel_after_body_returns(monkeypatch):
    """body 吞掉 fence 自发 cancel 并正常返回时，poller 仍收到 typed 失锁且继续运行。"""
    from swarm.infra import redis_client
    from swarm.knowledge.project_fence import (
        ProjectKnowledgeLeaseLost,
        project_knowledge_write_fence,
    )
    from swarm.project import store

    trace: list[str] = []

    class _LostLock:
        def __init__(self, *_args, ttl_sec=0.05, **_kwargs):
            self.ttl_sec = ttl_sec

        def acquire(self):
            return True

        def renew(self):
            return False

        def release(self):
            trace.append("release")

    monkeypatch.setattr(redis_client, "ModuleLock", _LostLock)
    monkeypatch.setattr(
        store,
        "get_project",
        lambda project_id: {"id": project_id, "status": "READY"},
    )

    async def _poll_once():
        try:
            async with project_knowledge_write_fence(
                "p-lost",
                operation="poll",
                renew_interval_seconds=0.01,
            ):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    trace.append("body_returned")
        except ProjectKnowledgeLeaseLost:
            trace.append("typed_loss")
        await asyncio.sleep(0)
        trace.append("poller_continued")

    await _poll_once()

    assert trace == ["body_returned", "release", "typed_loss", "poller_continued"]


@pytest.mark.asyncio
async def test_project_write_fence_preserves_external_cancellation(monkeypatch):
    """普通调用方取消不能被 fence 翻译成租约失败或清掉取消计数。"""
    from swarm.infra import redis_client
    from swarm.knowledge.project_fence import project_knowledge_write_fence
    from swarm.project import store

    entered = asyncio.Event()
    released: list[bool] = []

    class _HealthyLock:
        def __init__(self, *_args, ttl_sec=3600, **_kwargs):
            self.ttl_sec = ttl_sec

        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            released.append(True)

    monkeypatch.setattr(redis_client, "ModuleLock", _HealthyLock)
    monkeypatch.setattr(
        store,
        "get_project",
        lambda project_id: {"id": project_id, "status": "READY"},
    )

    async def _writer():
        async with project_knowledge_write_fence("p1", operation="external cancel"):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(_writer())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelling() == 1
    assert released == [True]


@pytest.mark.asyncio
async def test_project_write_fence_does_not_consume_concurrent_external_cancel(monkeypatch):
    """body 吞下租约 cancel 后又收到外部 cancel，退出仍须保持原取消语义。"""
    from swarm.infra import redis_client
    from swarm.knowledge.project_fence import project_knowledge_write_fence
    from swarm.project import store

    class _LostLock:
        def __init__(self, *_args, ttl_sec=0.05, **_kwargs):
            self.ttl_sec = ttl_sec

        def acquire(self):
            return True

        def renew(self):
            return False

        def release(self):
            return None

    monkeypatch.setattr(redis_client, "ModuleLock", _LostLock)
    monkeypatch.setattr(
        store,
        "get_project",
        lambda project_id: {"id": project_id, "status": "READY"},
    )

    async def _writer():
        async with project_knowledge_write_fence(
            "p1", operation="double cancel", renew_interval_seconds=0.01
        ):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                asyncio.current_task().cancel()

    task = asyncio.create_task(_writer())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelling() == 1


@pytest.mark.asyncio
async def test_mr_stale_snapshot_cannot_open_writer_after_project_deleted(monkeypatch):
    """MR 列表即使已抓取，锁内复读拒绝后也不得打开写连接。"""
    from swarm.knowledge import mr_history

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"iid": 1, "title": "old snapshot"}]

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def get(self, *_args, **_kwargs):
            return _Response()

        def close(self):
            pass

    @asynccontextmanager
    async def _rejecting_fence(*_args, **_kwargs):
        raise mr_history.ProjectKnowledgeUpdateRejected("deleted")
        yield  # pragma: no cover

    monkeypatch.setenv("SWARM_GITLAB_URL", "https://gitlab.invalid")
    monkeypatch.setenv("SWARM_GITLAB_TOKEN", "token")
    monkeypatch.setenv("SWARM_GITLAB_PROJECT_ID", "group/repo")
    monkeypatch.setattr(mr_history.httpx, "Client", _Client)
    monkeypatch.setattr(mr_history, "project_knowledge_write_fence", _rejecting_fence)
    opened: list[bool] = []

    def _must_not_open():
        opened.append(True)
        raise AssertionError("删除后的 MR 同步不得打开写连接")

    assert await mr_history.sync_mr_history_from_gitlab(_must_not_open, "p-deleted") == 0
    assert opened == []


@pytest.mark.asyncio
async def test_mr_changes_fetch_does_not_hold_project_default_lock(monkeypatch):
    """慢 GitLab /changes 请求不能占住项目执行与删除共用的 default lock。"""
    from swarm.infra import redis_client
    from swarm.knowledge import mr_history
    from swarm.project import store

    changes_started = threading.Event()
    allow_changes = threading.Event()

    class _Lock:
        owner = None
        guard = threading.Lock()

        def __init__(self, _project_id, _module_key, *, ttl_sec=3600, **_kwargs):
            self.ttl_sec = ttl_sec
            self.token = object()

        def acquire(self):
            with self.__class__.guard:
                if self.__class__.owner is not None:
                    return False
                self.__class__.owner = self.token
                return True

        def renew(self):
            with self.__class__.guard:
                return self.__class__.owner is self.token

        def release(self):
            with self.__class__.guard:
                if self.__class__.owner is self.token:
                    self.__class__.owner = None

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def get(self, url, **_kwargs):
            if url.endswith("/changes"):
                changes_started.set()
                assert allow_changes.wait(timeout=3)
                return _Response({"changes": [{"new_path": "src/a.py"}]})
            return _Response([
                {
                    "iid": 7,
                    "title": "slow changes",
                    "description": "",
                    "author": {"username": "dev"},
                    "state": "merged",
                    "web_url": "https://gitlab.invalid/mr/7",
                    "merged_at": None,
                }
            ])

        def close(self):
            return None

    writes: list[tuple] = []

    class _Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _sql, params):
            writes.append(tuple(params))

    class _Conn:
        def cursor(self):
            return _Cursor()

        async def close(self):
            return None

    monkeypatch.setenv("SWARM_GITLAB_URL", "https://gitlab.invalid")
    monkeypatch.setenv("SWARM_GITLAB_TOKEN", "token")
    monkeypatch.setenv("SWARM_GITLAB_PROJECT_ID", "group/repo")
    monkeypatch.setattr(redis_client, "ModuleLock", _Lock)
    monkeypatch.setattr(mr_history.httpx, "Client", _Client)
    monkeypatch.setattr(
        store,
        "get_project",
        lambda project_id: {"id": project_id, "status": "READY"},
    )

    syncing = asyncio.create_task(
        mr_history.sync_mr_history_from_gitlab(lambda: _Conn(), "p1", limit=50)
    )
    contender = _Lock("p1", "default")
    acquired = False
    try:
        assert await asyncio.to_thread(changes_started.wait, 1)
        acquired = contender.acquire()
        assert acquired is True
    finally:
        if acquired:
            contender.release()
        allow_changes.set()
        result = await asyncio.wait_for(syncing, timeout=3)

    assert result == 1
    assert len(writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["norm", "ingest", "requeue"])
async def test_knowledge_write_routes_map_project_fence_to_409(monkeypatch, endpoint):
    """规范和 Qdrant 采集在项目围栏拒绝后均不得触发实际写入。"""
    from fastapi import HTTPException
    from swarm.api.routers import knowledge as router

    @asynccontextmanager
    async def _rejecting_fence(*_args, **_kwargs):
        raise router.ProjectKnowledgeUpdateRejected("deleting")
        yield  # pragma: no cover

    monkeypatch.setattr(router, "_require_perm", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(router._app, "_validate_project", lambda _project_id: {})
    monkeypatch.setattr(router, "project_knowledge_write_fence", _rejecting_fence)
    writes: list[str] = []
    monkeypatch.setattr(
        router._app,
        "_get_pg_conn",
        lambda: writes.append("norm") or MagicMock(),
    )
    monkeypatch.setattr(router, "_build_ingest_source", lambda *_args: object())

    with pytest.raises(HTTPException) as caught:
        if endpoint == "norm":
            await router.create_norm(
                "p-deleting",
                object(),
                router.NormCreateRequest(title="n", content="c"),
            )
        elif endpoint == "ingest":
            await router.ingest_documents(
                "p-deleting",
                object(),
                router.IngestRequest(source_type="local", file_paths=["ignored"]),
            )
        else:
            await router.requeue_pending_embeddings("p-deleting", object())

    assert caught.value.status_code == 409
    assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fence_state", ["busy", "unavailable"])
async def test_embedding_retry_respects_project_fence_without_false_retry(
    monkeypatch,
    fence_state,
):
    """锁忙保留原条目不计失败；项目消失才清掉永久不可达条目。"""
    from swarm.knowledge import updater as updater_module
    from swarm.knowledge.project_fence import (
        ProjectKnowledgeUpdateRejected,
        ProjectKnowledgeWriterBusy,
    )

    @asynccontextmanager
    async def _fence(*_args, **_kwargs):
        if fence_state == "busy":
            raise ProjectKnowledgeWriterBusy("busy")
        raise ProjectKnowledgeUpdateRejected("gone")
        yield  # pragma: no cover

    monkeypatch.setattr(updater_module, "project_knowledge_write_fence", _fence)
    monkeypatch.setattr(updater_module, "_lookup_project_path", lambda _pid: "/tmp")
    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._conn = _FenceConnection(
        "READY",
        [("p-fenced", "src/a.py", "python")],
    )
    updater._lock = asyncio.Lock()

    class _Semantic:
        async def reindex_file_atomic(self, *_args, **_kwargs):
            raise AssertionError("围栏拒绝后不得触达 Qdrant")

    updater._semantic = _Semantic()

    assert await updater.retry_pending_embeddings() == 0
    statements = [
        " ".join(sql.split()) for sql, _ in updater._conn.cursor_obj.executed
    ]
    assert not any("retry_count = retry_count + 1" in sql for sql in statements)
    deleted = any(sql.startswith("DELETE FROM kb_pending_embeddings") for sql in statements)
    assert deleted is (fence_state == "unavailable")


@pytest.mark.asyncio
async def test_depgraph_triggered_inside_consumer_waits_for_outer_fence(monkeypatch):
    """事件消费持锁时触发的后台重建，必须等外层释放后最终执行一次。"""
    from swarm.knowledge import updater as updater_module
    from swarm.knowledge.project_fence import ProjectKnowledgeWriterBusy
    from swarm.project import codegraph, preprocess

    held = False

    @asynccontextmanager
    async def _non_reentrant_fence(project_id, *, operation):
        nonlocal held
        if held:
            raise ProjectKnowledgeWriterBusy("same project lock is held")
        held = True
        try:
            yield {"id": project_id, "status": "READY", "path": "/tmp/project"}
        finally:
            held = False

    monkeypatch.setattr(
        updater_module,
        "project_knowledge_write_fence",
        _non_reentrant_fence,
    )
    monkeypatch.setattr(codegraph, "is_codegraph_installed", lambda: True)
    monkeypatch.setattr(
        codegraph,
        "run_codegraph_full",
        lambda _path: SimpleNamespace(edges=[SimpleNamespace(source_file="a", target_file="b")]),
    )
    rebuilt = asyncio.Event()
    rebuild_calls: list[str] = []

    def _replace(project_id, _edges):
        rebuild_calls.append(project_id)
        rebuilt.set()

    monkeypatch.setattr(preprocess, "_replace_dependency_graph", _replace)

    payload = {"changes": [], "metadata": {}}
    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._conn = _FenceConnection("READY", [(8, "p1", payload)])
    updater._lock = asyncio.Lock()
    updater._kb_config = SimpleNamespace(depgraph_rebuild_threshold=1)
    updater._depgraph_dirty = {}
    updater._depgraph_tasks = set()

    async def _handle(event):
        await updater._maybe_rebuild_depgraph(event.project_id)
        await asyncio.sleep(0)
        return {}

    updater.handle_event = _handle
    updater._maybe_reconcile_stuck = lambda: asyncio.sleep(0)

    assert await updater.process_pending_events() == 1
    await asyncio.wait_for(rebuilt.wait(), timeout=0.5)
    assert rebuild_calls == ["p1"]


@pytest.mark.asyncio
async def test_depgraph_busy_uses_capped_exponential_backoff(monkeypatch):
    """长期 writer 争用不能退化成固定高频锁探测与日志风暴。"""
    from swarm.knowledge import updater as updater_module
    from swarm.knowledge.project_fence import ProjectKnowledgeWriterBusy

    attempts = 0

    @asynccontextmanager
    async def _eventually_available(project_id, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 8:
            raise ProjectKnowledgeWriterBusy("busy")
        yield {"id": project_id, "status": "READY", "path": "/tmp/project"}

    delays: list[float] = []

    async def _record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(updater_module, "project_knowledge_write_fence", _eventually_available)
    monkeypatch.setattr(updater_module.asyncio, "sleep", _record_sleep)
    monkeypatch.setattr("swarm.project.codegraph.is_codegraph_installed", lambda: True)
    monkeypatch.setattr(
        "swarm.project.codegraph.run_codegraph_full",
        lambda _path: SimpleNamespace(edges=[]),
    )
    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._lock = asyncio.Lock()

    await updater._rebuild_depgraph_async("p-busy")

    assert delays == [0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0]


@pytest.mark.asyncio
async def test_updater_close_drains_depgraph_before_closing_indexes():
    """shutdown 必须等被取消的重建任务 finally 收尾后再关闭其依赖。"""
    trace: list[str] = []
    started = asyncio.Event()

    async def _depgraph_task():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            trace.append("depgraph_drained")

    class _Closable:
        async def close(self):
            trace.append("index_closed")

    task = asyncio.create_task(_depgraph_task())
    await started.wait()
    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._depgraph_tasks = {task}
    updater._struct = _Closable()
    updater._semantic = None
    updater._behavior = None
    updater._conn = None

    await updater.close()

    assert trace == ["depgraph_drained", "index_closed"]
    assert task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("competing_writer", ["events", "embeddings"])
async def test_depgraph_and_connection_writer_use_one_lock_order_without_deadlock(
    monkeypatch,
    tmp_path,
    competing_writer,
):
    """depgraph 持项目锁跑图时，下一轮连接写者不得持全局锁反等项目锁。"""
    from swarm.knowledge import updater as updater_module
    from swarm.project import codegraph, preprocess

    project_lock = asyncio.Lock()
    depgraph_has_project_lock = asyncio.Event()
    writer_waiting_for_project = asyncio.Event()
    codegraph_started = threading.Event()
    release_codegraph = threading.Event()

    @asynccontextmanager
    async def _ordered_fence(project_id, *, operation):
        if operation != "依赖图重建":
            writer_waiting_for_project.set()
        async with project_lock:
            if operation == "依赖图重建":
                depgraph_has_project_lock.set()
            yield {
                "id": project_id,
                "status": "READY",
                "path": str(tmp_path),
            }

    monkeypatch.setattr(
        updater_module,
        "project_knowledge_write_fence",
        _ordered_fence,
    )
    monkeypatch.setattr(codegraph, "is_codegraph_installed", lambda: True)

    def _blocked_codegraph(_path):
        codegraph_started.set()
        assert release_codegraph.wait(timeout=2)
        return SimpleNamespace(
            edges=[SimpleNamespace(source_file="a.py", target_file="b.py")]
        )

    monkeypatch.setattr(codegraph, "run_codegraph_full", _blocked_codegraph)
    monkeypatch.setattr(preprocess, "_replace_dependency_graph", lambda *_args: None)

    payload = {"changes": [], "metadata": {}}
    pending_rows = (
        [(11, "p1", payload)]
        if competing_writer == "events"
        else [("p1", "a.py", "python")]
    )

    updater = KnowledgeUpdater.__new__(KnowledgeUpdater)
    updater._lock = asyncio.Lock()
    updater._conn = _FenceConnection("READY", pending_rows)
    updater._kb_config = SimpleNamespace(depgraph_rebuild_threshold=50)
    updater._depgraph_tasks = set()
    updater._depgraph_dirty = {}
    updater._maybe_reconcile_stuck = lambda: asyncio.sleep(0)
    updater.handle_event = lambda _event: asyncio.sleep(0, result={"errors": []})

    original_execute = updater._conn.cursor_obj.execute

    async def _assert_connection_serialized(sql, params=None):
        assert updater._lock.locked(), "共享 AsyncConnection 访问必须持 updater lock"
        await original_execute(sql, params)

    updater._conn.cursor_obj.execute = _assert_connection_serialized

    class _Semantic:
        async def reindex_file_atomic(self, *_args, **_kwargs):
            return None

    updater._semantic = _Semantic()
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    depgraph = asyncio.create_task(updater._rebuild_depgraph_async("p1"))
    await depgraph_has_project_lock.wait()
    assert await asyncio.to_thread(codegraph_started.wait, 1)
    writer = asyncio.create_task(
        updater.process_pending_events()
        if competing_writer == "events"
        else updater.retry_pending_embeddings()
    )
    try:
        await asyncio.wait_for(writer_waiting_for_project.wait(), timeout=3)
        release_codegraph.set()
        await asyncio.wait_for(asyncio.gather(depgraph, writer), timeout=5)
    finally:
        release_codegraph.set()
        for task in (depgraph, writer):
            if not task.done():
                task.cancel()
        await asyncio.gather(depgraph, writer, return_exceptions=True)

    assert not updater._lock.locked()
