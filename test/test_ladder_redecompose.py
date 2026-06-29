#!/usr/bin/env python3
"""卡死子任务恢复阶梯·阶梯二：定点拆小（_targeted_redecompose）。

多文件卡死子任务（耗尽重试+有成功兄弟）→ escalate 前先拆小（复用 _resplit_subtask），
保留成功兄弟、只重派小块，不全盘 FAILED。单/双文件拆不动 → 返回 None 交阶梯三。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.brain.nodes import _targeted_redecompose
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput


def _st(sid, writable, depends_on=None):
    return SubTask(id=sid, description=f"建 {sid}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable), depends_on=depends_on or [])


def _wo(sid, ok=True):
    return WorkerOutput(subtask_id=sid, diff="d" if ok else "", summary="", l1_passed=ok,
                        l1_details={}, confidence="high" if ok else "low")


def _run(coro):
    return asyncio.run(coro)


def test_multifile_stuck_subtask_redecomposed_preserving_siblings():
    """多文件卡死 X(4 文件) + 成功兄弟 → 拆小、X 出完成态、子块入待派、保留兄弟。"""
    # X 多文件（4），有成功兄弟 st-1
    X = _st("st-2", ["a.java", "b.java", "c.java", "d.java"])
    plan = TaskPlan(subtasks=[_st("st-1", ["s1.java"]), X])
    state = {
        "plan": plan,
        "subtask_results": {"st-1": _wo("st-1"), "st-2": _wo("st-2", ok=False)},
        "dispatch_remaining": [],
        "subtask_redecompose_count": {},
    }
    # mock _resplit_subtask 拆成 2 个小块
    children = [_st("st-2-1", ["a.java", "b.java"]), _st("st-2-2", ["c.java", "d.java"])]
    with patch("swarm.brain.planning_nodes._resplit_subtask",
               new=_async_return(children)), \
         patch("swarm.brain.planning_nodes._oversized_by_files", return_value=False):
        out = _run(_targeted_redecompose(state, "st-2"))
    assert out is not None, "多文件卡死应被拆小"
    assert out["failure_strategy"] == "retry"
    new_ids = [s.id for s in out["plan"].subtasks]
    assert "st-2" not in new_ids and "st-2-1" in new_ids and "st-2-2" in new_ids
    assert "st-1" in out["subtask_results"], "成功兄弟必须保留"
    assert "st-2" not in out["subtask_results"], "卡死的 X 出完成态待重做"
    assert "st-2-1" in out["dispatch_remaining"] and "st-2-2" in out["dispatch_remaining"]
    assert out["subtask_redecompose_count"]["st-2"] == 1


def test_single_file_stuck_subtask_not_redecomposed():
    """单文件卡死 → 拆不动 → 返回 None（交阶梯三）。"""
    X = _st("st-2", ["only.java"])
    plan = TaskPlan(subtasks=[_st("st-1", ["s1.java"]), X])
    state = {"plan": plan, "subtask_results": {"st-1": _wo("st-1"), "st-2": _wo("st-2", ok=False)},
             "dispatch_remaining": [], "subtask_redecompose_count": {}}
    out = _run(_targeted_redecompose(state, "st-2"))
    assert out is None, "单文件拆不动应返回 None 交阶梯三"


def test_already_redecomposed_not_again():
    """已拆过 1 次 → 不再拆（有界）。"""
    X = _st("st-2", ["a.java", "b.java", "c.java"])
    plan = TaskPlan(subtasks=[X])
    state = {"plan": plan, "subtask_results": {}, "dispatch_remaining": [],
             "subtask_redecompose_count": {"st-2": 1}}
    out = _run(_targeted_redecompose(state, "st-2"))
    assert out is None, "已拆过的不应再拆（防无限）"


def test_resplit_cant_split_returns_none():
    """_resplit_subtask 拆不动（返回 1 个）→ None 交阶梯三。"""
    X = _st("st-2", ["a.java", "b.java", "c.java"])
    plan = TaskPlan(subtasks=[X])
    state = {"plan": plan, "subtask_results": {}, "dispatch_remaining": [],
             "subtask_redecompose_count": {}}
    with patch("swarm.brain.planning_nodes._resplit_subtask", new=_async_return([X])), \
         patch("swarm.brain.planning_nodes._oversized_by_files", return_value=False):
        out = _run(_targeted_redecompose(state, "st-2"))
    assert out is None


def _async_return(val):
    async def _f(*a, **k):
        return val
    return _f


if __name__ == "__main__":
    import sys
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v()
            except Exception as e:  # noqa: BLE001
                import traceback
                print(f"  ❌ {k}: {e}")
                traceback.print_exc()
                fails += 1
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
