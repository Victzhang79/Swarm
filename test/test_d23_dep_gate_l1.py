#!/usr/bin/env python3
"""D23 治本单测 —— 依赖闸门只把【L1 通过】的结果当"已完成"。

旧 bug：`completed_ids = set(subtask_results.keys())` + `_is_ready` 只查 key 存在 → 滞留的
L1 未通过失败结果被当"已完成"满足下游 depends_on → 下游提前派发（上游从未真正成功）→ 空烧。
治本：`completed_l1_ids(subtask_results)` 只计 l1_passed 为真者（消费 WorkerOutput 单一事实源），
dispatch 与 after_monitor 熔断探测同口径。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.nodes.shared import completed_l1_ids  # noqa: E402
from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


def _sub(sid, deps=None):
    return SubTask(
        id=sid, description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[]), depends_on=deps or [],
    )


def _out(sid, l1):
    return WorkerOutput(subtask_id=sid, diff="x", summary="", confidence=Confidence.HIGH, l1_passed=l1)


def test_completed_l1_ids_filters_failed():
    results = {"st-up": _out("st-up", l1=False), "st-ok": _out("st-ok", l1=True)}
    assert completed_l1_ids(results) == {"st-ok"}
    # 旧口径会把 st-up 也算进去，坐实差异
    assert set(results.keys()) == {"st-up", "st-ok"}
    print("  ✅ completed_l1_ids 只计 L1 通过者(排除滞留失败)")


def test_downstream_not_ready_when_upstream_l1_failed():
    plan = TaskPlan(subtasks=[_sub("st-up"), _sub("st-down", deps=["st-up"])])
    # 上游 L1 未过但结果滞留在 subtask_results
    results = {"st-up": _out("st-up", l1=False)}
    completed = completed_l1_ids(results)
    batch = plan.get_dispatch_batch(completed, ["st-down"], max_concurrent=4)
    assert [t.id for t in batch] == [], "上游 L1 未过时下游不得就绪派发"
    print("  ✅ 上游 L1 未过 → 下游不 ready(不提前派发空烧)")


def test_downstream_ready_when_upstream_l1_passed():
    plan = TaskPlan(subtasks=[_sub("st-up"), _sub("st-down", deps=["st-up"])])
    results = {"st-up": _out("st-up", l1=True)}
    completed = completed_l1_ids(results)
    batch = plan.get_dispatch_batch(completed, ["st-down"], max_concurrent=4)
    assert [t.id for t in batch] == ["st-down"], "上游 L1 通过后下游应就绪"
    print("  ✅ 上游 L1 通过 → 下游就绪派发")


def test_completed_l1_ids_handles_dict_and_none():
    # 鸭子判超集：dict 形态与 None 也正确处理。
    results = {"a": {"l1_passed": True}, "b": {"l1_passed": False}, "c": None}
    assert completed_l1_ids(results) == {"a"}
    assert completed_l1_ids({}) == set()
    print("  ✅ dict/None 形态处理正确")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("D23 全部通过")
