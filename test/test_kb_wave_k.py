"""Wave K 低风险修复回归测试。

覆盖:
- A-P1-20  retrieve_knowledge 崩溃路径置 retrieval_failed 哨兵
- A-P1-22  preprocess KB 写入用连接池 + executemany（不再裸 psycopg.connect）
- A-P1-24  MR /changes 非 200 不静默落空（记录失败并跳过）
- A-P1-25  预处理完成但 0 索引/0 嵌入 → degraded 而非 ready
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from swarm.knowledge.readiness import assess_knowledge_readiness


# ── A-P1-26 learn_store 事务 ───────────────────────────────
def test_learn_success_step2_failure_rolls_back(monkeypatch):
    """step-2(write_task_summary) 失败 → 整个事务回滚，step-1(write_success)被撤销。

    用一个记录式 fake store：transaction() 进入时快照，__aexit__ 收到异常则把
    success 写入"回滚"（移除）。write_task_summary 抛错 → 验证 success 未持久。
    """
    import asyncio
    from contextlib import asynccontextmanager

    from swarm.brain import learn_store

    persisted: dict[str, list] = {"success": [], "task_summary": []}
    in_txn_writes: list[tuple] = []

    class _FakeStore:
        async def connect(self):
            pass

        async def close(self):
            pass

        async def query_successes(self, *a, **k):
            return []

        async def query_mistakes(self, *a, **k):
            return []

        def transaction(self):
            @asynccontextmanager
            async def _cm():
                in_txn_writes.clear()
                try:
                    yield None
                except Exception:
                    # 回滚：丢弃本事务内的写
                    in_txn_writes.clear()
                    raise
                else:
                    # 提交：落库
                    for kind, val in in_txn_writes:
                        persisted[kind].append(val)
            return _cm()

        async def write_success(self, project_id, entry):
            in_txn_writes.append(("success", 1))
            return 1

        async def write_task_summary(self, project_id, summary):
            raise RuntimeError("step-2 boom")

    monkeypatch.setattr(learn_store, "MemoryStore", _FakeStore)

    state = {
        "project_id": "proj-1",
        "task_id": "t1",
        "task_description": "add feature",
        "complexity": "medium",
        "merged_diff": "diff",
    }
    meta = asyncio.run(learn_store.persist_learn_success(state, {
        "pattern_name": "p",
        "pattern_description": "d",
        "applicable_scenarios": [],
    }))
    assert meta["persisted"] is False
    # step-1 不应留下孤儿 success
    assert persisted["success"] == [], persisted
    assert persisted["task_summary"] == []


# ── A-P1-20 ───────────────────────────────────────────────
def test_retrieve_knowledge_crash_sets_retrieval_failed():
    """检索整体崩溃时返回空知识，并显式置 retrieval_failed=True + error。"""
    import asyncio

    from swarm.knowledge import service

    async def _boom():
        raise RuntimeError("qdrant down")

    with patch.object(service, "get_retriever", side_effect=RuntimeError("qdrant down")):
        ctx, stats = asyncio.run(
            service._retrieve_knowledge_impl("任务", "proj-1")
        )
    assert stats.get("retrieval_failed") is True
    assert "qdrant down" in stats.get("error", "")
    # 空知识但能与"真无知识"区分
    assert ctx["struct"] == []


# ── A-P1-22 ───────────────────────────────────────────────
def test_save_file_index_uses_pool_and_executemany():
    """_save_file_index 走 sync_pool().connection() + executemany，不再裸 connect。"""
    from swarm.project import preprocess

    cur = MagicMock()
    conn_cm = MagicMock()
    conn_cm.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value = conn_cm

    files = [
        {"rel_path": "a.py", "language": "python", "hash": "h1", "lines": 10, "abs_path": "/a.py"},
        {"rel_path": "b.py", "language": "python", "hash": "h2", "lines": 20, "abs_path": "/b.py"},
    ]
    with patch.object(preprocess, "logger"), \
         patch("swarm.infra.db.sync_pool", return_value=pool) as mock_pool:
        preprocess._save_file_index("proj-1", files)

    mock_pool.assert_called_once()
    cur.executemany.assert_called_once()
    # 批量：第二个参数是 2 行
    rows = cur.executemany.call_args[0][1]
    assert len(rows) == 2
    cur.execute.assert_not_called()


def test_save_symbol_index_empty_noop():
    """空符号列表直接返回，不取连接。"""
    from swarm.project import preprocess

    with patch("swarm.infra.db.sync_pool") as mock_pool:
        preprocess._save_symbol_index("proj-1", [])
    mock_pool.assert_not_called()


# ── A-P1-24 ───────────────────────────────────────────────
def test_mr_changes_non_200_not_silently_empty(monkeypatch):
    """/changes 返回 429 → 该 MR 跳过(不落空 changed_files)，不计入 count。"""
    import asyncio

    from swarm.knowledge import mr_history

    monkeypatch.setenv("SWARM_GITLAB_URL", "https://gl.example.com")
    monkeypatch.setenv("SWARM_GITLAB_TOKEN", "tok")
    monkeypatch.setenv("SWARM_GITLAB_PROJECT_ID", "42")

    mrs = [{"iid": 1, "title": "t1"}]

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise mr_history.httpx.HTTPStatusError(
                    "err", request=MagicMock(), response=MagicMock(status_code=self.status_code)
                )

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        def get(self, url, **kw):
            if url.endswith("/merge_requests"):
                return _Resp(200, mrs)
            # /changes → 429
            return _Resp(429, {})

        def close(self):
            pass

    # 不应执行任何 INSERT（唯一的 MR 因 /changes 429 被跳过）
    cur = MagicMock()
    cur.execute = MagicMock()

    class _AsyncCur:
        async def __aenter__(self):
            return cur

        async def __aexit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _AsyncCur()

        async def close(self):
            pass

    async def _exec(*a, **k):
        return None

    cur_async = MagicMock()
    cur_async.execute = _exec

    class _AsyncCur2:
        async def __aenter__(self):
            return cur_async

        async def __aexit__(self, *a):
            return False

    class _Conn2:
        def cursor(self):
            return _AsyncCur2()

        async def close(self):
            pass

    monkeypatch.setattr(mr_history.httpx, "Client", _Client)

    count = asyncio.run(
        mr_history.sync_mr_history_from_gitlab(lambda: _Conn2(), "proj-1", limit=10)
    )
    # 唯一 MR 的 /changes 失败 → 跳过 → count == 0（绝不静默写空 changed_files）
    assert count == 0


# ── A-P1-25 ───────────────────────────────────────────────
def test_readiness_zero_counts_is_degraded_not_ready():
    """phase=complete 但 0 符号 / 0 向量 → degraded（不是 ready）。"""
    r = assess_knowledge_readiness(
        {"status": "READY"},
        {"phase": "complete", "index_stats": {"symbols": 0}, "embed_stats": {"vectors": 0}},
    )
    assert r["level"] == "degraded", r


def test_readiness_nonzero_counts_is_ready():
    """有符号/向量 → ready。"""
    r = assess_knowledge_readiness(
        {"status": "READY"},
        {"phase": "complete", "index_stats": {"symbols": 120}, "embed_stats": {"vectors": 120}},
    )
    assert r["level"] == "ready", r


def test_readiness_skipped_still_partial():
    """显式 skipped 仍是 partial（不被 degraded 抢占）。"""
    r = assess_knowledge_readiness(
        {"status": "READY"},
        {"phase": "complete", "index_stats": {"skipped": True}, "embed_stats": {"vectors": 0}},
    )
    assert r["level"] == "partial", r
