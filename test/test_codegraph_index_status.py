"""P1-21 回归：codegraph 索引失败/部分不得被标 graph_status=INDEXED。

_phase_index 据 CodegraphResult.ok 判终态：ok=True(含真空项目)→ INDEXED；
ok=False(init/index 失败、db 缺失、解析异常)→ DEGRADED。纯逻辑，DB/CLI 全 mock。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.project.codegraph import CodegraphResult
from swarm.project import preprocess


def _run_phase_index_with(cg_result: CodegraphResult) -> str:
    """跑 _phase_index，返回捕获到的 graph_status。"""
    captured = {}

    def _fake_update_project(project_id, **kw):
        if "graph_status" in kw:
            captured["graph_status"] = kw["graph_status"]

    with patch.object(preprocess, "_check_codegraph", return_value=True), \
         patch.object(preprocess, "_run_codegraph", return_value=cg_result), \
         patch.object(preprocess, "_save_symbol_index"), \
         patch.object(preprocess, "_save_dependency_graph"), \
         patch.object(preprocess, "_prune_absent_files", return_value=0), \
         patch("swarm.project.store.update_project", _fake_update_project), \
         patch("swarm.project.store.upsert_progress"):
        asyncio.run(preprocess._phase_index("_test_p1_21", "/tmp/_test_p1_21"))
    return captured.get("graph_status")


def test_codegraph_failure_marked_degraded_not_indexed():
    failed = CodegraphResult(ok=False, error="init failed: boom")
    assert _run_phase_index_with(failed) == "DEGRADED"


def test_codegraph_empty_but_ok_marked_indexed():
    # 成功但空项目(0 符号) → 仍 INDEXED，不回归。
    empty_ok = CodegraphResult(ok=True)
    assert empty_ok.symbol_count == 0
    assert _run_phase_index_with(empty_ok) == "INDEXED"


def test_codegraph_success_with_symbols_indexed():
    ok = CodegraphResult(symbol_count=42, edge_count=7, ok=True)
    assert _run_phase_index_with(ok) == "INDEXED"


def test_degraded_is_valid_graph_status_enum():
    """DEGRADED 必须是 GraphStatus 合法成员，否则前端/CLI 显示枚举外值 / Project 模型校验失败。"""
    from swarm.project.models import GraphStatus, Project

    assert GraphStatus("DEGRADED") is GraphStatus.DEGRADED
    # Project 模型能接受 DEGRADED（typed as GraphStatus），不抛校验错。
    p = Project(id="x", name="n", path="/tmp/x", graph_status="DEGRADED")
    assert p.graph_status is GraphStatus.DEGRADED
