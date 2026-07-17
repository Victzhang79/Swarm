"""R65TR-T4③⑤：终态未核验账——推迟到从未到达的复核层的验收项必入机读账。

治后回放实证两条同源盲区（皆"验收推迟给 L2/复核，但 PARTIAL 未到 L2 = 静默丢失"）：
- ⑤ C2 遵约对账 ~15 次全推迟"L2 D5 将全局复核"（契约符号未出现在 diff），
  每次写 l1_details["contract_missing_symbols"]，但终态无聚合——L2 从未运行时
  这些"可能已存在，待全局复核"的悬置项无终态可见。
- ③ st-24-3 描述要求新建 AlarmEscalationService、acceptance 5 条引用其方法，但
  create_files=[]、verify_commands=None → L1 打 needs_review=no_test_or_verify_commands
  后编译过即放行；NL 验收从未被确定性核过。

治=终态未核验账（纯聚合已在 state 的 per-subtask l1_details 键，不设闸、零假阳、
栈中立）：仅当 L2 未运行（无 l2_details）时，_failed_machine_account 聚合
contract_missing_symbols + needs_review(no_test/skipped) → tu["acceptance_unverified"]。
L2 跑过则 D5 已全局对账，不重复报。
"""

from __future__ import annotations

import logging

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput


def _st(sid):
    return SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=[f"m/{sid}.java"]), depends_on=[])


def _wo(sid, **l1):
    return WorkerOutput(subtask_id=sid, diff="+x", summary="", l1_passed=True,
                        l1_details=l1)


def _state(l2_passed=None):
    plan = TaskPlan(subtasks=[_st("st-c2"), _st("st-nl"), _st("st-clean")],
                    parallel_groups=[["st-c2", "st-nl", "st-clean"]])
    st = {
        "plan": plan,
        "subtask_results": {
            "st-c2": _wo("st-c2", contract_missing_symbols=["AlarmRecord", "AlarmCallbackLog"]),
            "st-nl": _wo("st-nl", needs_review="no_test_or_verify_commands"),
            "st-clean": _wo("st-clean", deterministic_gate="pass"),
        },
        "subtask_dispatch_totals": {"st-c2": 1, "st-nl": 1, "st-clean": 1},
    }
    if l2_passed is not None:
        st["l2_passed"] = l2_passed  # verify_l2 每分支都设，L2 运行过的真信号
    return st


def test_terminal_accounts_deferred_c2_and_nl_when_no_l2(caplog):
    from swarm.brain.runner import _failed_machine_account
    with caplog.at_level(logging.WARNING):
        tu = _failed_machine_account("t-1", _state(l2_passed=None), "rejected_partial")
    acc = tu.get("acceptance_unverified")
    assert acc, f"L2 未运行时推迟验收项必入终态账: {tu}"
    assert acc.get("contract_missing", {}).get("st-c2") == ["AlarmRecord", "AlarmCallbackLog"], acc
    assert "st-nl" in (acc.get("nl_acceptance_only") or []), acc
    assert "st-clean" not in (acc.get("nl_acceptance_only") or []), "干净子任务不入账"
    assert any("未核验" in r.getMessage() or "acceptance_unverified" in r.getMessage()
               for r in caplog.records), "未核验账必须 WARNING 留痕"


def test_no_account_when_l2_passed_no_l2_details():
    """★猎手 F1 回归★：L2 运行且通过=真实成功形态（l2_passed=True 但 l2_details 缺席，
    verify_l2 通过分支从不设 l2_details）——旧 l2_details 门会误报"L2 未到达"。"""
    from swarm.brain.runner import _failed_machine_account
    tu = _failed_machine_account("t-2", _state(l2_passed=True), "rejected_partial")
    assert "acceptance_unverified" not in tu, \
        f"L2 通过（l2_passed=True/l2_details 缺席）绝不能误报未核验: {tu}"


def test_no_account_when_l2_failed():
    """L2 运行但失败 → 也算 L2 到达过（merged 契约/集成检查已发生），不重复报。"""
    from swarm.brain.runner import _failed_machine_account
    tu = _failed_machine_account("t-2b", _state(l2_passed=False), "rejected_partial")
    assert "acceptance_unverified" not in tu, tu


def test_checkpoint_dict_form_subtask_results_not_dropped():
    """★猎手 F2 回归★：subtask_results 为 checkpoint 还原的 plain dict 时（salvage 路径
    正是本特性动因）——不得静默丢账。"""
    from swarm.brain.runner import _failed_machine_account
    st = {
        "plan": TaskPlan(subtasks=[_st("st-c2")], parallel_groups=[["st-c2"]]),
        # WorkerOutput 序列化后的 dict 形态（checkpoint values）
        "subtask_results": {
            "st-c2": {"subtask_id": "st-c2", "diff": "+x", "l1_passed": True,
                      "l1_details": {"contract_missing_symbols": ["AlarmRecord"]}},
        },
        "subtask_dispatch_totals": {"st-c2": 1},
    }
    tu = _failed_machine_account("t-4", st, "rejected_partial")
    acc = tu.get("acceptance_unverified") or {}
    assert acc.get("contract_missing", {}).get("st-c2") == ["AlarmRecord"], \
        f"dict 形态子任务结果不得被静默丢账: {tu}"


def test_no_account_when_nothing_deferred():
    from swarm.brain.runner import _failed_machine_account
    plan = TaskPlan(subtasks=[_st("st-clean")], parallel_groups=[["st-clean"]])
    st = {
        "plan": plan,
        "subtask_results": {"st-clean": _wo("st-clean", deterministic_gate="pass")},
        "subtask_dispatch_totals": {"st-clean": 1},
    }
    tu = _failed_machine_account("t-3", st, "rejected_partial")
    assert "acceptance_unverified" not in tu, tu


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
