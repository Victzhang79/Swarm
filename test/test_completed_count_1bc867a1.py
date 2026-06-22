"""治本 task 1bc867a1：WebUI 概览 completed 35 > count 34。

根因：_sync_task_from_state 用 len(subtask_results) 当 completed——该 dict 累积了跨
replan/retry/rebase 的全部结果（含失败 + st-N-2 重生成变体 + 已不在当前 plan 的旧 id），
必然超过当前 plan 的 subtask_count。正解：只数【当前 plan 内 且 L1 通过】的子任务，并夹紧到 count。
"""
from __future__ import annotations

from unittest.mock import patch

import swarm.brain.runner as runner
from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput


def _out(sid, passed):
    return WorkerOutput(subtask_id=sid, diff="", summary="", l1_passed=passed)


def _capture_sync(state):
    captured = {}

    def _fake_update(task_id, **kw):
        captured.update(kw)

    with patch.object(runner.store, "update_task", _fake_update):
        runner._sync_task_from_state("t1", state)
    return captured


def test_completed_excludes_failed_and_stale_variants():
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d", scope=FileScope()),
        SubTask(id="st-2", description="d", scope=FileScope()),
        SubTask(id="st-3", description="d", scope=FileScope()),
    ])
    state = {
        "plan": plan,
        "subtask_results": {
            "st-1": _out("st-1", True),    # 当前 plan 内 + 通过 → 计
            "st-2": _out("st-2", True),    # 计
            "st-3": _out("st-3", False),   # 失败 → 不计
            "st-2-2": _out("st-2-2", True),  # rebase 变体，不在当前 plan → 不计
            "st-old": _out("st-old", True),  # 旧 replan 残留 id → 不计
        },
    }
    cap = _capture_sync(state)
    assert cap["subtask_count"] == 3
    assert cap["completed_subtasks"] == 2, cap  # 只有 st-1/st-2
    assert cap["completed_subtasks"] <= cap["subtask_count"]


def test_completed_never_exceeds_count_clamp():
    """即便结果数虚高，completed 也夹紧到 subtask_count。"""
    plan = TaskPlan(subtasks=[SubTask(id="st-1", description="d", scope=FileScope())])
    # 构造极端：同 id 不可能重复（dict），但用多个 plan 内通过 + 变体来确保 ≤ count
    state = {
        "plan": plan,
        "subtask_results": {
            "st-1": _out("st-1", True),
            "st-1-2": _out("st-1-2", True),  # 变体，不在 plan
            "st-9": _out("st-9", True),       # 旧 id
        },
    }
    cap = _capture_sync(state)
    assert cap["subtask_count"] == 1
    assert cap["completed_subtasks"] == 1, cap


def test_no_plan_falls_back_to_passed_count():
    """state 无 plan 时退化为数 L1 通过的结果（仍优于 len 全量）。"""
    state = {"subtask_results": {"a": _out("a", True), "b": _out("b", False)}}
    cap = _capture_sync(state)
    assert cap["completed_subtasks"] == 1, cap


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
