"""P1-21 回归：codegraph 索引失败/部分不得被标 graph_status=INDEXED。

_phase_index 据 CodegraphResult.ok 判终态：ok=True(含真空项目)→ INDEXED；
ok=False(init/index 失败、db 缺失、解析异常)→ DEGRADED。纯逻辑，DB/CLI 全 mock。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from contextlib import contextmanager

from swarm.project.codegraph import CodegraphResult
from swarm.project import preprocess


def _run_phase_index_with(cg_result: CodegraphResult) -> tuple[str, object, object]:
    """跑 _phase_index，返回捕获到的 graph_status。"""
    captured = {}

    def _fake_update_project(project_id, **kw):
        if "graph_status" in kw:
            captured["graph_status"] = kw["graph_status"]

    with patch.object(preprocess, "_check_codegraph", return_value=True), \
         patch.object(preprocess, "_run_codegraph", return_value=cg_result), \
         patch.object(preprocess, "_replace_symbol_index") as replace_symbols, \
         patch.object(preprocess, "_replace_dependency_graph") as replace_edges, \
         patch.object(preprocess, "_prune_absent_files", return_value=0), \
         patch("swarm.project.store.update_project", _fake_update_project), \
         patch("swarm.project.store.upsert_progress"):
        asyncio.run(preprocess._phase_index("_test_p1_21", "/tmp/_test_p1_21"))
    return captured.get("graph_status"), replace_symbols, replace_edges


def test_codegraph_failure_marked_degraded_not_indexed():
    failed = CodegraphResult(ok=False, error="init failed: boom")
    status, replace_symbols, replace_edges = _run_phase_index_with(failed)
    assert status == "DEGRADED"
    replace_symbols.assert_not_called()
    replace_edges.assert_not_called()


def test_codegraph_empty_but_ok_marked_indexed():
    # 成功但空项目(0 符号) → 仍 INDEXED，不回归。
    empty_ok = CodegraphResult(ok=True)
    assert empty_ok.symbol_count == 0
    status, replace_symbols, replace_edges = _run_phase_index_with(empty_ok)
    assert status == "INDEXED"
    replace_symbols.assert_called_once_with("_test_p1_21", [])
    replace_edges.assert_called_once_with("_test_p1_21", [])


def test_codegraph_success_with_symbols_indexed():
    ok = CodegraphResult(symbol_count=42, edge_count=7, ok=True)
    status, replace_symbols, replace_edges = _run_phase_index_with(ok)
    assert status == "INDEXED"
    replace_symbols.assert_called_once()
    replace_edges.assert_called_once()


def test_degraded_is_valid_graph_status_enum():
    """DEGRADED 必须是 GraphStatus 合法成员，否则前端/CLI 显示枚举外值 / Project 模型校验失败。"""
    from swarm.project.models import GraphStatus, Project

    assert GraphStatus("DEGRADED") is GraphStatus.DEGRADED
    # Project 模型能接受 DEGRADED（typed as GraphStatus），不抛校验错。
    p = Project(id="x", name="n", path="/tmp/x", graph_status="DEGRADED")
    assert p.graph_status is GraphStatus.DEGRADED


def test_replace_symbol_index_empty_really_deletes_old_rows():
    calls = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params): calls.append((sql.strip(), params))
        def executemany(self, *args): raise AssertionError("空集不应 INSERT")

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def cursor(self): return Cursor()
        @contextmanager
        def transaction(self):
            yield

    class Pool:
        @contextmanager
        def connection(self):
            yield Conn()

    with patch("swarm.infra.db.sync_pool", return_value=Pool()):
        preprocess._replace_symbol_index("p-empty", [])

    assert calls == [(
        "DELETE FROM kb_symbol_index WHERE project_id = %s",
        ("p-empty",),
    )]
