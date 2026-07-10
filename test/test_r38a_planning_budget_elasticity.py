"""R38-A（round38 治本 #1）：规划期预算弹性——STAGE1 揭示模块数后按 per_module×n 放宽。

round38（task 21d80f40）死因主干：ledger 弹性预算 = base + per_subtask×子任务数，而
子任务数是【规划的输出】——规划期恒为 0，ultra 任务规划流水线（9 模块两阶段设计+契约+
抽取+拆批 ≳25 次重型调用）只有 base×plan_ratio=125k 可用，7/9 模块设计被
TaskTokenLimitExceeded 拒绝（总花费才 27k/500k=5.4%）。round27 实测 ULTRA 仅规划期
云端 >800k（settings.py max_task_tokens_per_subtask 注释），佐证 125k 尺寸结构性错误。

治本（对齐 P1-B 墙钟"规划揭示后动态重算放宽"既有模式）：
  - ledger.widen_budget()：单调只增不减的弹性放宽原语（防后到的小值把已放宽预算收缩回去）。
  - TECH_DESIGN-STAGE1 产出模块清单后【立即、在 STAGE2 发起前】按
    base + max_task_tokens_per_module×模块数 放宽——runner 的 on_chain_end 钩子在节点
    结束才触发，救不了死在 tech_design 节点内部的 STAGE2。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from swarm.models import ledger
from swarm.models.errors import TaskTokenLimitExceeded


@pytest.fixture(autouse=True)
def _clean_ledger(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    yield
    ledger._reset_for_tests()


# ─────────────────── widen_budget 原语 ───────────────────

def test_widen_budget_only_increases():
    """弹性放宽单调只增不减：后到的小值不得把已放宽预算收缩回去。"""
    ledger.attach("w1", budget_total=500_000)
    ledger.widen_budget("w1", 2_300_000)
    assert ledger.snapshot("w1")["budget_total"] == 2_300_000
    ledger.widen_budget("w1", 800_000)  # 更小 → no-op
    assert ledger.snapshot("w1")["budget_total"] == 2_300_000


def test_widen_budget_zero_or_negative_is_noop():
    ledger.attach("w2", budget_total=500_000)
    ledger.widen_budget("w2", 0)
    ledger.widen_budget("w2", -1)
    assert ledger.snapshot("w2")["budget_total"] == 500_000


def test_widen_budget_never_enables_gate_on_trackonly_task():
    """budget=0（关闸/track-only）语义不被弹性放宽意外开启——关闸是显式运维决策。"""
    ledger.attach("w3", budget_total=0)
    ledger.widen_budget("w3", 2_000_000)
    assert ledger.snapshot("w3")["budget_total"] == 0


def test_reattach_does_not_shrink_widened_budget():
    """resume/重复 attach（runner 每执行段都 attach，规划期 subtasks=0 → base）
    不得把 STAGE1 已 widen 的预算缩回去——弹性只增不减跨执行段成立。"""
    ledger.attach("w4", budget_total=500_000)
    ledger.widen_budget("w4", 2_300_000)
    ledger.attach("w4", budget_total=500_000)  # resume 再 attach base
    assert ledger.snapshot("w4")["budget_total"] == 2_300_000


def test_reattach_zero_explicitly_disables_gate():
    """显式关闸（budget=0）必须赢——运维决策优先于弹性保留。"""
    ledger.attach("w5", budget_total=500_000)
    ledger.widen_budget("w5", 2_300_000)
    ledger.attach("w5", budget_total=0)
    assert ledger.snapshot("w5")["budget_total"] == 0


def test_widened_budget_survives_db_reload(monkeypatch):
    """跨进程重启：widen 后 flush 落库 budget_total，fresh attach 从 DB 恢复不缩水。"""
    ledger._reset_for_tests()
    saved: dict = {}
    monkeypatch.setattr(ledger, "_flush_row", lambda tid, row: saved.update({tid: row}) or True)
    monkeypatch.setattr(ledger, "_load_row", lambda tid: None)
    ledger.attach("w6", budget_total=500_000)
    ledger.widen_budget("w6", 2_300_000)
    ledger.flush()
    assert saved["w6"].get("budget_total") == 2_300_000
    # 模拟重启：内存清空，_load_row 回放 DB 行
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda tid: dict(saved.get(tid) or {}))
    ledger.attach("w6", budget_total=500_000)
    assert ledger.snapshot("w6")["budget_total"] == 2_300_000


# ─────────────────── round38 现场复现：放宽后 STAGE2 并发预留全过 ───────────────────

def test_round38_stage2_concurrency_survives_after_module_widen():
    """round38 死点复现：plan 阶段 spent≈25k + 并发 3×大预留在 125k 子预算下第 3 个必拒；
    按模块数放宽后（base+9×per_module=2.3M → plan 子预算 575k）三个并发预留全部放行。"""
    ledger.attach("t38", budget_total=500_000)
    ledger.set_stage("t38", "plan")
    # STAGE1 已花 ~25k（plan 阶段）
    rid = ledger.reserve("t38", est_in=20_000, est_out=5_000, kind="cloud")
    ledger.settle(rid, real_in=20_000, real_out=5_000)

    # 未放宽（round38 现场）：第 3 个并发预留撞 plan 子预算 125k
    ledger.reserve("t38", est_in=36_000, est_out=4_096, kind="cloud")
    ledger.reserve("t38", est_in=36_000, est_out=4_096, kind="cloud")
    with pytest.raises(TaskTokenLimitExceeded) as ei:
        ledger.reserve("t38", est_in=36_000, est_out=4_096, kind="cloud")
    assert ei.value.usage.get("stage") == "plan"

    # STAGE1 揭示 9 模块 → 放宽 base + 9×200k
    ledger.widen_budget("t38", 500_000 + 9 * 200_000)
    # 此前被拒的同规格预留现在放行
    ledger.reserve("t38", est_in=36_000, est_out=4_096, kind="cloud")


# ─────────────────── 配置面 ───────────────────

def test_config_has_max_task_tokens_per_module():
    """新配置 max_task_tokens_per_module（SWARM_MAX_TASK_TOKENS_PER_MODULE）：
    默认 >0（ultra 规划期弹性默认生效，round27/38 两轮实证规划期被掐死），0=关闭。"""
    from swarm.config.settings import AppConfig
    cfg = AppConfig()
    per_mod = getattr(cfg, "max_task_tokens_per_module", None)
    assert per_mod is not None, "缺 max_task_tokens_per_module 配置字段"
    assert per_mod > 0


# ─────────────────── STAGE1 → 放宽时序（在 STAGE2 发起前）───────────────────

class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """第 1 次 ainvoke 返回 STAGE1 顶层方案（9 模块）；后续返回各模块 file_plan。
    STAGE2 调用发生时断言 widen 已被调用（时序：放宽必须先于 STAGE2 发起）。"""

    def __init__(self, widen_calls: list):
        self._n = 0
        self._widen_calls = widen_calls

    async def ainvoke(self, messages):
        self._n += 1
        if self._n == 1:
            modules = [{"name": f"mod-{i}", "responsibility": "r", "est_files": 2}
                       for i in range(9)]
            return _FakeResp(json.dumps({
                "modules": modules, "architecture": "arch", "data_model": "dm",
                "fact_issues": [], "shared_contract": {},
            }))
        assert self._widen_calls, "STAGE2 已发起但预算尚未按模块数放宽（时序错误）"
        return _FakeResp(json.dumps({
            "file_plan": [{"path": f"src/f{self._n}.x", "action": "create",
                           "description": "d"}],
        }))


def test_tech_design_stage1_widens_budget_before_stage2(monkeypatch):
    from swarm.brain import planning_nodes as pn

    widen_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "swarm.models.ledger.widen_budget",
        lambda task_id, budget: widen_calls.append((task_id, budget)))

    cfg = type("Cfg", (), {"max_task_tokens": 500_000,
                           "max_task_tokens_per_module": 200_000})()
    monkeypatch.setattr(pn, "get_config", lambda: cfg)

    state = {"task_id": "t-widen", "clarify_summary": ""}
    result, file_plan, fact_issues, contract = asyncio.run(
        pn._tech_design_staged(
            _FakeLLM(widen_calls), "task desc", "ultra", True, state,
            "facts", "", ""))

    assert widen_calls, "STAGE1 揭示模块数后未放宽预算"
    tid, budget = widen_calls[0]
    assert tid == "t-widen"
    assert budget == 500_000 + 9 * 200_000
    assert len(file_plan) == 9  # 9 模块全部产出（无预算拒绝）


def test_tech_design_no_widen_when_no_modules(monkeypatch):
    """STAGE1 未按格式给模块（退回单次路径）→ 不放宽（无规模信号不放大预算）。"""
    from swarm.brain import planning_nodes as pn

    widen_calls: list = []
    monkeypatch.setattr(
        "swarm.models.ledger.widen_budget",
        lambda task_id, budget: widen_calls.append((task_id, budget)))
    cfg = type("Cfg", (), {"max_task_tokens": 500_000,
                           "max_task_tokens_per_module": 200_000})()
    monkeypatch.setattr(pn, "get_config", lambda: cfg)

    class _NoModuleLLM:
        async def ainvoke(self, messages):
            return _FakeResp(json.dumps({"file_plan": [
                {"path": "src/a.x", "action": "create", "description": "d"}]}))

    state = {"task_id": "t-nomod", "clarify_summary": ""}
    asyncio.run(pn._tech_design_staged(
        _NoModuleLLM(), "task desc", "medium", True, state, "facts", "", ""))
    assert not widen_calls
