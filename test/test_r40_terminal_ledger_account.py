#!/usr/bin/env python3
"""R40-2（round40 治本批）—— 三终态统一带 ledger 权威账。

取证：round40 PARTIAL 终态 token_usage 只有 degraded_summary，stage_spent/
llm_calls/cloud in-out 全缺——ledger 快照此前只在 FAILED 路径合并
（_failed_machine_account），PARTIAL/DONE 无账。治本：合并收编进三终态唯一
共同出口 _attach_observability_account（只补缺失键绝不覆写）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

SNAP = {"cloud_tokens_in": 413616, "cloud_tokens_out": 769199, "local_tokens": 0,
        "llm_calls": 46, "stage_spent": {"plan": 1173323}, "budget_total": 8000000}


def test_attach_merges_ledger_snapshot(monkeypatch):
    from swarm.brain import runner
    from swarm.models import ledger
    monkeypatch.setattr(ledger, "snapshot", lambda tid: dict(SNAP) if tid == "t1" else {})
    tu: dict = {}
    runner._attach_observability_account(tu, {"task_id": "t1"})
    assert tu["stage_spent"] == {"plan": 1173323}, "PARTIAL/DONE 也要有阶段分账"
    assert tu["llm_calls"] == 46 and tu["budget_total"] == 8000000


def test_attach_never_overwrites_existing(monkeypatch):
    """FAILED 路径先填过的键（含合法 0 值）不被快照覆写。"""
    from swarm.brain import runner
    from swarm.models import ledger
    monkeypatch.setattr(ledger, "snapshot", lambda tid: dict(SNAP))
    tu = {"llm_calls": 99, "cloud_tokens_in": 0}
    runner._attach_observability_account(tu, {"task_id": "t1"})
    assert tu["llm_calls"] == 99 and tu["cloud_tokens_in"] == 0
    assert tu["stage_spent"] == {"plan": 1173323}, "缺失键照常补"


def test_attach_no_task_id_or_snapshot_failure_safe(monkeypatch):
    from swarm.brain import runner
    from swarm.models import ledger
    tu: dict = {}
    runner._attach_observability_account(tu, {})  # 无 task_id → 跳过不炸
    assert "stage_spent" not in tu

    def _boom(tid):
        raise RuntimeError("db down")
    monkeypatch.setattr(ledger, "snapshot", _boom)
    tu2: dict = {}
    runner._attach_observability_account(tu2, {"task_id": "t1"})  # 快照炸 → 不阻断
    assert "stage_spent" not in tu2
