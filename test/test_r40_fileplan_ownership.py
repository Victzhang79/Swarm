#!/usr/bin/env python3
"""R40-1（round40 治本批）—— file_plan 归属确定性闸 + 缺件外科修复。

取证（TASK_REGISTER R40-1）：round40 PARTIAL 直接死因=file_plan 43 文件里 3 个无
owner 子任务（AlarmTaskServiceImpl/AlarmTaskChannelServiceImpl=BLOCKED"无生产者的
包"核心实现类 + alarm_core_ddl.sql）——批拆丢件在规划期无任何校验，执行期才以
BLOCKED→连坐放弃形态爆发。
治本（零 LLM）：
  (a) validate_file_plan_ownership：file_plan 文件无任何子任务 create_files/writable
      认领 → 打回（带具体缺件清单 D09 回灌）；basename 已被认领的孪生件豁免为 warn
      （P5 同名去重防误伤，round39 教训）；空 file_plan/单子任务跳过。
  (b) maybe_file_plan_repair：缺件类校验失败重试 → deepcopy 上一版 plan，缺件按
      模块+路径前缀深度确定性挂到同模块子任务 create_files；修不好如实 None 回退。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_validator import validate_file_plan_ownership  # noqa: E402
from swarm.brain.symbol_surgery import maybe_file_plan_repair  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, desc="", writable=None, create=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


FP = [
    "mod-a/src/main/java/com/x/AService.java",
    "mod-a/src/main/java/com/x/impl/AServiceImpl.java",
    "mod-a/src/main/resources/sql/ddl.sql",
]


# ── (a) 闸门 ──

def test_missing_files_bounce_with_list():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/main/java/com/x/AService.java"]),
        _st("st-2", create=["mod-a/src/main/java/com/x/other/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    r = validate_file_plan_ownership(plan, FP)
    assert not r.valid
    blob = " ".join(r.issues)
    assert "AServiceImpl.java" in blob and "ddl.sql" in blob, "缺件清单必须具体到路径"
    assert len(r.issues) == 2, "逐条 issue（一件一 bullet）让 D09 A9 分页轮转生效"


def test_all_owned_passes():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=list(FP)),
        _st("st-2", create=["mod-a/src/main/java/com/x/other/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    r = validate_file_plan_ownership(plan, FP)
    assert r.valid and not r.issues


def test_p5_dedup_single_source_of_truth():
    """口径同源（复核 HIGH 修 → R67-1 收权升级）：分母仍与 P5 dedupe_file_plan 同一事实源，
    但 P5 只剪完全同路径——跨路径同名孪生件全部留在分母，失主必报（旧 basename 全局剪
    曾把 12 个 UI 模板剪出分母 → 本闸静默放行假完整，round67 "审计跟着剪除者走"实锤）。"""
    fp = [
        "mod-a/src/main/java/com/x/AService.java",
        "mod-b/src/main/java/com/y/AService.java",   # R67-1：同名孪生留在分母，失主必报
        "mod-a/pom.xml",
        "mod-b/pom.xml",                             # 每模块一份，各自硬性
    ]
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/main/java/com/x/AService.java", "mod-a/pom.xml"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    r = validate_file_plan_ownership(plan, fp)
    blob = " ".join(r.issues)
    assert not r.valid and "mod-b/pom.xml" in blob, "每模块一份的文件缺 owner=真缺件"
    assert "mod-b/src/main/java/com/y/AService.java" in blob, (
        "R67-1：跨路径同名孪生件必须留在分母，失主必报（账不得被剪除者抹掉）")


def test_empty_or_single_subtask_skipped():
    single = TaskPlan(subtasks=[_st("st-1", create=["a/B.java"])],
                      parallel_groups=[["st-1"]])
    assert validate_file_plan_ownership(single, FP).valid, "单子任务计划跳过（SIMPLE 面）"
    multi = TaskPlan(subtasks=[_st("st-1"), _st("st-2", create=["a/B.java"])],
                     parallel_groups=[["st-1", "st-2"]])
    assert validate_file_plan_ownership(multi, []).valid, "空 file_plan 跳过"


# ── (b) 外科修复 ──

def _state(plan, issues_text):
    return {"plan": plan,
            "tech_design_file_plan": list(FP),
            "plan_validation_feedback": issues_text,
            "plan_validation_issues": [issues_text],
            "replan_feedback": "", "plan_batch_failed_modules": []}


def test_repair_attaches_missing_to_module_peer():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/main/java/com/x/AService.java"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    r0 = validate_file_plan_ownership(plan, FP)
    assert not r0.valid
    state = _state(plan, r0.issues[0])
    repaired = maybe_file_plan_repair(state)
    assert repaired is not None, "缺件类失败+同模块有候选 → 必须外科不重拆"
    assert validate_file_plan_ownership(repaired, FP).valid, "修后闸必过"
    st1 = next(st for st in repaired.subtasks if st.id == "st-1")
    assert "mod-a/src/main/java/com/x/impl/AServiceImpl.java" in st1.scope.create_files, (
        "缺件挂到同模块（路径前缀最深匹配）子任务")
    # 原 plan 零污染
    assert "mod-a/src/main/java/com/x/impl/AServiceImpl.java" not in \
        plan.subtasks[0].scope.create_files


def test_repair_declines_when_no_module_candidate():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-zzz/src/Z.java"]),
    ], parallel_groups=[["st-1"]])
    r0 = validate_file_plan_ownership(
        TaskPlan(subtasks=[_st("a"), _st("b", create=["mod-zzz/src/Z.java"])],
                 parallel_groups=[["a", "b"]]), FP)
    state = _state(plan, r0.issues[0] if r0.issues else "file_plan 文件无 owner 子任务")
    assert maybe_file_plan_repair(state) is None, "无同模块候选 → 诚实回退全量重拆"


def test_repair_ignores_non_fileplan_failures():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/main/java/com/x/AService.java"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    state = _state(plan, "覆盖缺口：req-123 未被 covers")
    assert maybe_file_plan_repair(state) is None, "非缺件类失败不越权"
