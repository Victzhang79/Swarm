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
    """图上每个业务节点都必须有阶段归属（漏映射=该节点花费归错阶段）。"""
    import re
    src = open("brain/graph.py").read()
    nodes = set(re.findall(r'graph\.add_node\("([^"]+)"', src))
    assert nodes, "未解析到图节点"
    missing = {n for n in nodes if ledger.stage_for_node(n) is None}
    assert missing == set(), f"图节点缺阶段映射: {missing}"


def test_stage_ratios_sum_at_most_one():
    assert sum(ledger.DEFAULT_STAGE_RATIOS.values()) <= 1.0 + 1e-9


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
