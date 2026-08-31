#!/usr/bin/env python3
"""tech_design 阶段2 外层超时 × Brain 主备链预算的确定性排序。

round64 实锤（cassette seq6）：ruoyi-framework 思考失控（28841 reasoning chunk 零正文），
写死 500s 的节点 wait_for 在 R56-1 无损切备预算（600s）之前抢跑 → 闸结构性够不着 →
白烧 500s + 盲目同模型重试。外层必须晚于 primary 与 fallback 各自的完整墙钟预算。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _timeout_with_budget(monkeypatch, budget, wallclock=0.0):
    import swarm.brain.planning_nodes as pn

    class _ModelCfg:
        brain_reasoning_phase_budget_s = budget
        brain_stream_wallclock_s = wallclock

    class _Cfg:
        model = _ModelCfg()

    monkeypatch.setattr(pn, "get_config", lambda: _Cfg())
    return pn._stage2_module_timeout()


def test_timeout_exceeds_reasoning_budget(monkeypatch):
    """★round64 seq6 本体★ 预算 600s → 节点超时必须 ≥ 720s，让 R56-1 无损切备先触发。"""
    assert _timeout_with_budget(monkeypatch, 600.0) == 720.0


def test_timeout_preserves_fallback_fresh_wallclock_budget(monkeypatch):
    """主模型最坏跑满墙钟后，备用仍须拥有一份完整墙钟预算，外层不得抢先取消。"""
    assert _timeout_with_budget(monkeypatch, 600.0, wallclock=1500.0) == 3120.0


def test_timeout_keeps_floor_when_budget_disabled(monkeypatch):
    """预算关闭（0）→ 保持 500s 原值（无闸可让，行为零回归）。"""
    assert _timeout_with_budget(monkeypatch, 0.0) == 500.0


def test_timeout_keeps_floor_when_budget_below(monkeypatch):
    """预算 300s < 500s 地板 → R56-1 天然先触发，超时保持地板不虚增。"""
    assert _timeout_with_budget(monkeypatch, 300.0) == 500.0


def test_timeout_none_budget_safe(monkeypatch):
    """配置缺字段/None → 安全回退 500s（不炸不虚增）。"""
    assert _timeout_with_budget(monkeypatch, None) == 500.0
