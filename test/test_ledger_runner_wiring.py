"""阶段1.4（§九 TaskLedger）：runner 接线纯逻辑面 — 行为测试。

  - stage 映射：brain 图全部业务节点都有归属阶段（新增节点漏映射→保持上一阶段，
    不误闸但要被本测试抓漏）。
  - 弹性预算口径与 token 闸同源（base+per_subtask×n；base=0 关闸）。
  - replan 轮次按 state 绝对值同步（只增不减，resume 恢复值不被回退）。
  - detach 结算段墙钟并写穿。
"""

from __future__ import annotations

import pytest

from swarm.brain.runner import _ledger_effective_budget
from swarm.models import ledger


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    yield
    ledger._reset_for_tests()


def test_stage_map_covers_all_graph_nodes():
    """图上每个业务节点都必须有阶段归属（漏映射=该节点花费归错阶段）。

    R63-T11：改吃 GRAPH_NODE_REGISTRY 单一事实源——此前正则扫 add_node 字面量，
    注册形态一变（表驱动重构）就静默解析出空集。"""
    from swarm.brain.graph import GRAPH_NODE_REGISTRY

    nodes = {n for n, _ in GRAPH_NODE_REGISTRY}
    assert len(nodes) >= 26, f"图节点数异常（{len(nodes)}），注册表疑似被截: {sorted(nodes)}"
    missing = {n for n in nodes if ledger.stage_for_node(n) is None}
    assert missing == set(), f"图节点缺阶段映射: {missing}"


def test_stage_ratios_are_independent_caps():
    """R38b-1 ③ 语义演进：比例是各阶段【独立上限】（各自 ×budget_total），非分配份额
    ——守恒由总闸负责，允许重叠（plan 0.25→0.35 后总和 1.10）。上限仍须各自 ≤1 且 >0。"""
    for k, v in ledger.DEFAULT_STAGE_RATIOS.items():
        assert 0 < v <= 1.0, f"stage {k} ratio {v} 越界"
    # 借位顶格后单阶段有效上限也不得超过总预算
    for k, v in ledger.DEFAULT_STAGE_RATIOS.items():
        assert v * ledger._STAGE_BORROW_CAP <= 1.0 + 1e-9, (
            f"stage {k}: ratio {v} × 借位顶格 {ledger._STAGE_BORROW_CAP} 超过总预算")


def test_effective_budget_math():
    class _Cfg:
        max_task_tokens = 500_000
        max_task_tokens_per_subtask = 150_000

    assert _ledger_effective_budget(_Cfg(), 0) == 500_000
    assert _ledger_effective_budget(_Cfg(), 10) == 2_000_000
    _Cfg.max_task_tokens = 0
    assert _ledger_effective_budget(_Cfg(), 10) == 0, "base=0 必须保持关闸语义"


def test_set_replan_rounds_monotonic():
    ledger.attach("w1", budget_total=0)
    ledger.set_replan_rounds("w1", 2)
    ledger.set_replan_rounds("w1", 1)  # 不回退（resume 恢复的历史值不被小值覆盖）
    assert ledger.snapshot("w1")["replan_rounds"] == 2
    ledger.set_replan_rounds("w1", 3)
    assert ledger.snapshot("w1")["replan_rounds"] == 3


def test_detach_settles_wall_ms_and_flushes(monkeypatch):
    written: list = []
    monkeypatch.setattr(ledger, "_flush_row",
                        lambda tid, row: (written.append((tid, dict(row))), True)[1])
    ledger.attach("w2", budget_total=100)
    ledger.detach("w2")
    assert written and written[-1][0] == "w2"
    assert written[-1][1]["wall_ms"] >= 0
    assert ledger.snapshot("w2") == {}, "detach 后出内存（DB 留档）"
