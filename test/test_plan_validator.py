#!/usr/bin/env python3
"""P0 — PlanValidator 单元测试"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _st(
    sid: str,
    *,
    writable: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> SubTask:
    return SubTask(
        id=sid,
        description=sid,
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [f"{sid}.py"], readable=[]),
        depends_on=depends_on or [],
    )


def test_valid_plan_passes():
    plan = TaskPlan(
        subtasks=[_st("a"), _st("b", depends_on=["a"])],
        parallel_groups=[["a"], ["b"]],
    )
    r = validate_plan_structure(plan)
    assert r.valid, r.issues


def test_cycle_detected():
    plan = TaskPlan(
        subtasks=[
            _st("a", depends_on=["b"]),
            _st("b", depends_on=["a"]),
        ],
        parallel_groups=[["a", "b"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid
    assert any("循环" in i for i in r.issues)


def test_parallel_writable_conflict():
    plan = TaskPlan(
        subtasks=[
            _st("a", writable=["shared.py"]),
            _st("b", writable=["shared.py"]),
        ],
        parallel_groups=[["a", "b"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid
    assert any("并行冲突" in i or "同时写" in i for i in r.issues)


def test_max_writable_files():
    plan = TaskPlan(
        subtasks=[_st("a", writable=["f1.py", "f2.py", "f3.py", "f4.py"])],
        parallel_groups=[["a"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid
    assert any("超过上限" in i for i in r.issues)


def test_unknown_dependency():
    plan = TaskPlan(
        subtasks=[_st("a", depends_on=["missing"])],
        parallel_groups=[["a"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid
    assert any("未知任务" in i for i in r.issues)


if __name__ == "__main__":
    test_valid_plan_passes()
    test_cycle_detected()
    test_parallel_writable_conflict()
    test_max_writable_files()
    test_unknown_dependency()
    print("test_plan_validator: all passed")
