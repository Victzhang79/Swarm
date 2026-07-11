#!/usr/bin/env python3
"""R39-5（round39 治本批）—— 校验失败类型分流：符号类走确定性外科，不全量重拆。

取证（R39-1）：round39 重试 2/3 时覆盖已满足 → P1 让路（nodes:1802）→ 符号类失败
只能全量重拆，三轮缺口 71→71→68 不动白烧 2×15min LLM 批拆。
治本：maybe_symbol_repair 闸门——仅【符号类/规则5 校验失败重试】启用，deepcopy
上一版 plan 后确定性修复（R39-4 脚手架注入 + R39-2 符号挂靠），C1 同口径复核
通过才放行；修不好如实 None 回退全量重拆（结构类失败的正当出口）。
守卫对齐 P1（F-3：执行失败 replan 必须真跑；整模块分解失败绝不外科）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_validator import validate_contract_ownership  # noqa: E402
from swarm.brain.symbol_surgery import maybe_symbol_repair  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)

C1_MSG = "契约符号无 owner 子任务承接 5/8（占比 62% 超阈值 40%）: IAService"


def _st(sid, desc="", create=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=[], create_files=create or []))


def _fixable_state():
    """符号类失败 + 可确定性修复：接口有模块归属且同模块有子任务。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [
        {"name": "IAService", "module": "mod-a"},
        {"name": "IBService", "module": "mod-b"},
        {"name": "ICService", "module": "mod-b"},
    ]}
    return {
        "plan": plan,
        "shared_contract": sc,
        "plan_validation_feedback": C1_MSG,
        "plan_validation_issues": [C1_MSG],
        "replan_feedback": "",
        "plan_batch_failed_modules": [],
    }


def test_symbol_failure_repaired_without_resplit():
    state = _fixable_state()
    old_ids = [st.id for st in state["plan"].subtasks]
    repaired = maybe_symbol_repair(state)
    assert repaired is not None, "符号类失败+可修 → 必须走外科不重拆"
    assert [st.id for st in repaired.subtasks][:2] == old_ids, "复用上一版子任务"
    r = validate_contract_ownership(repaired, state["shared_contract"])
    assert r.valid, "外科后 C1 同口径复核必须通过"
    # deepcopy 纪律：外科绝不半改 state 里的原 plan（修不好回退时原版必须干净）
    assert not state["plan"].subtasks[0].contract.get("symbols"), (
        "原 plan 对象不得被就地污染")


def test_coverage_type_failure_not_taken():
    state = _fixable_state()
    state["plan_validation_feedback"] = "覆盖缺口：req-123 未被任何子任务 covers"
    state["plan_validation_issues"] = [state["plan_validation_feedback"]]
    assert maybe_symbol_repair(state) is None, "覆盖类归 P1，符号外科不越权"


def test_replan_feedback_guard_f3():
    state = _fixable_state()
    state["replan_feedback"] = "执行失败 replan：st-2 连续 3 轮 empty_diff"
    assert maybe_symbol_repair(state) is None, "执行失败 replan 必须真跑（F-3）"


def test_batch_failed_modules_guard():
    state = _fixable_state()
    state["plan_batch_failed_modules"] = ["mod-b"]
    assert maybe_symbol_repair(state) is None, "缺整模块时外科救不了，回退全量重拆"


def test_unfixable_returns_none_and_clean():
    """全部符号无模块归属 → 挂不上 → C1 仍超阈值 → 如实 None（回退全量重拆）。"""
    state = _fixable_state()
    state["shared_contract"] = {"interfaces": [
        {"name": f"IOrphan{i}Service", "module": ""} for i in range(5)]}
    assert maybe_symbol_repair(state) is None
    assert not state["plan"].subtasks[0].contract.get("symbols"), (
        "修复失败时原 plan 零污染")


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_SYMBOL_SURGERY", "0")
    assert maybe_symbol_repair(_fixable_state()) is None, "泄压阀关=旧行为"
