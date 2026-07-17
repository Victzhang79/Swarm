"""R65REPLAY-T6（task #71）：终态账务守恒闸——"派发过却无账"的软掉账必入机读账。

round65d 回放轮 C 路独家发现+主线程裁决：dispatch_totals 33 个 id vs subtask_results
28 → st-2/st-23-1/st-17-1/st-27/st-11-1(拆前旧id) 被派发过却零记账。机制=HANDLE_FAILURE
重派路径 subtask_results.pop(fid)（意图=待重派覆盖），但调度层（#70 饿死）永不兑现 →
终态既无失败账也无完成账，挂 remaining 永冻——"第五态"。W3 处置总账（#60）只保证帧内
平账，跨帧"重派承诺是否兑现"无人对账。

治本（收敛裁决）：不动 pop 语义（presence 被 monitor/dispatch 广泛消费，重派意图合法；
调度兑现本体归 #70）——治=终态对账：_failed_machine_account（FAILED/PARTIAL(rejected/
governor) 共用，回放轮 rejected_partial 实际路径）以 subtask_dispatch_totals（单调终身
账，state.py:312 豁免剪枝）为权威，凡 totals 有记录、results 无条目、且非放弃者 →
tu["dispatched_unaccounted"] 机读列出 + WARNING。plan 外旧 id（重拆前 st-11-1 形态）
同列（派发是事实）。栈中立（纯账务）。
"""
from __future__ import annotations

import logging

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput


def _st(sid):
    return SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=[f"m/{sid}.java"]), depends_on=[])


def _state():
    plan = TaskPlan(subtasks=[_st("st-ok"), _st("st-lost"), _st("st-ab")],
                    parallel_groups=[["st-ok", "st-lost", "st-ab"]])
    return {
        "plan": plan,
        "subtask_results": {
            "st-ok": WorkerOutput(subtask_id="st-ok", diff="+x", summary="",
                                  l1_passed=True),
        },
        # st-lost：派发过、账被 pop、未放弃 → 软掉账；st-ab：派发过但已放弃（有账）；
        # st-old-1：plan 外旧 id（重拆前父任务）——派发是事实，同列。
        "subtask_dispatch_totals": {"st-ok": 1, "st-lost": 1, "st-ab": 2, "st-old-1": 1},
        "abandoned_subtask_ids": ["st-ab"],
    }


def test_failed_machine_account_lists_unaccounted(caplog):
    """★软掉账本体★：totals 有/results 无/非放弃 → 必入 dispatched_unaccounted。"""
    from swarm.brain.runner import _failed_machine_account
    with caplog.at_level(logging.WARNING):
        tu = _failed_machine_account("t-1", _state(), "rejected_partial")
    got = tu.get("dispatched_unaccounted")
    assert got == ["st-lost", "st-old-1"], \
        f"派发过却无账的子任务必须入终态机读账（回放轮 5 个软掉账死型）: {got}"
    assert any("dispatched_unaccounted" in r.message or "无账" in r.message
               for r in caplog.records), "软掉账必须 WARNING 留痕"


def test_failed_machine_account_no_key_when_clean():
    """全账平（有 results 或已放弃）→ 不发键（有才发键约定）。"""
    from swarm.brain.runner import _failed_machine_account
    st = _state()
    st["subtask_dispatch_totals"] = {"st-ok": 1, "st-ab": 2}
    tu = _failed_machine_account("t-1", st, "rejected")
    assert "dispatched_unaccounted" not in tu


def test_failed_machine_account_none_state_safe():
    """state=None（既有调用形态）不崩不发键。"""
    from swarm.brain.runner import _failed_machine_account
    tu = _failed_machine_account("t-1", None, "rejected")
    assert "dispatched_unaccounted" not in tu
