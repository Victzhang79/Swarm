"""A5（2026-07-09 深读登记册·阶段0）：H1 产出判定纳入 allow_any / delete_files — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §二 A5：
  - _build_simple_plan 在"开放式需求/检索未精确命中文件"时产出 writable=[] +
    allow_any=True（worker 自行定位目标文件，scope_guard 放行任意路径）。
  - validate_plan_structure 的 H1 产出闸只看 writable∪create_files → 该计划被判
    "无产出"确定性拒绝；SIMPLE 是确定性构造，重试三次产出同一计划=三连败任务死。
  - 治本：allow_any=True 的子任务【能】产出改动（这正是它的语义），纳入 writers 判定。
  - 同类 sibling：纯删除子任务（仅 delete_files）也产出真实 diff，同样不该被 H1 误杀
    （shared.py _is_pure_delete 证明纯删除 scope 是合法形态）。

栈无关：抽象子任务，无语言/项目词汇。
"""

from __future__ import annotations

from swarm.brain.nodes.shared import _build_simple_plan
from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


def _plan_with_scope(scope):
    return TaskPlan(
        subtasks=[SubTask(id="st-1", description="do something",
                          difficulty=SubTaskDifficulty.MEDIUM, scope=scope)],
        parallel_groups=[["st-1"]],
    )


def test_allow_any_subtask_counts_as_writer():
    """allow_any=True（worker 可写任意路径）→ 计划有产出能力，H1 必须放行。"""
    r = validate_plan_structure(_plan_with_scope(
        FileScope(writable=[], readable=[], allow_any=True)))
    assert r.valid, f"allow_any 计划被误判无产出（SIMPLE 确定性三连败根因）: {r.issues}"


def test_pure_delete_subtask_counts_as_writer():
    """纯删除子任务（仅 delete_files）产出真实 diff → H1 不得误杀。"""
    r = validate_plan_structure(_plan_with_scope(
        FileScope(writable=[], readable=[], delete_files=["obsolete/x"])))
    assert r.valid, f"纯删除计划被误判无产出: {r.issues}"


def test_truly_empty_plan_still_rejected():
    """全空 scope 且无 allow_any → 仍确定性拒绝（H1 假绿门回归保护）。"""
    r = validate_plan_structure(_plan_with_scope(
        FileScope(writable=[], readable=[])))
    assert not r.valid


def test_build_simple_plan_open_ended_passes_structure_gate():
    """集成：开放式需求的 SIMPLE 计划（allow_any 形态）必须过结构闸，不再确定性三连败。"""
    plan = _build_simple_plan("做一个开放式的小改进", [], project_path=None)
    scope = plan.subtasks[0].scope
    assert scope.allow_any is True and not scope.writable  # 前提成立（A5 病理形态）
    r = validate_plan_structure(plan)
    assert r.valid, f"SIMPLE allow_any 计划过不了结构闸: {r.issues}"
