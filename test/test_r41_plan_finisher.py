#!/usr/bin/env python3
"""R41（round41 治本批）—— PLAN 确定性收尾器：外科通道互斥病根的组合修复。

取证（task 3740e421，2026-07-12 FAILED@PLAN，2h22min 0 执行）：
1. 直接死因：最后一轮重试的校验失败同时携带【覆盖缺口 + file_plan 孤儿】两类
   issue，P1 覆盖外科抢跑（first-match-wins：task_plan is None 才轮到下一通道），
   R40-1 缺件外科全程零触发——一个 `sql/alarm_notice_read.sql` 无 owner 带病重验，
   重试耗尽 → CONFIRM auto_accept fail-fast 拒绝 → FAILED。
2. 次生：R39-4 脚手架注入只接线在符号外科内部；符号外科修不了硬符号如实回退时，
   注入随被丢弃的候选一起蒸发（02:18:46.049 注入 11 模块 → 全量重拆冲掉），
   规则5 预警 11 模块贯穿三轮原样复现。
治本：finish_plan_deterministic 在 PLAN 后处理区统一跑（任何产出路径），
  ①脚手架注入 ②孤儿挂靠（fail-open：挂不上的留 VALIDATE 权威打回）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_finisher import finish_plan_deterministic  # noqa: E402
from swarm.brain.plan_validator import validate_file_plan_ownership  # noqa: E402
from swarm.brain.symbol_surgery import (  # noqa: E402
    attach_orphan_file_plan_entries,
    maybe_file_plan_repair,
)
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


# ── ① 共享内核：孤儿挂靠 ──

def test_orphan_attach_round41_death_scenario():
    """round41 真死因复现：sql/ 模块孤儿文件必须挂到已有 sql 文件的子任务。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["alarm-task/src/main/java/com/x/A.java"]),
        _st("st-sql", create=["sql/alarm_task.sql", "sql/alarm_channel.sql"]),
    ], parallel_groups=[["st-1", "st-sql"]])
    fp = ["alarm-task/src/main/java/com/x/A.java", "sql/alarm_task.sql",
          "sql/alarm_channel.sql", "sql/alarm_notice_read.sql"]
    attached, left = attach_orphan_file_plan_entries(plan, fp)
    assert attached == 1 and not left
    assert "sql/alarm_notice_read.sql" in plan.subtasks[1].scope.create_files
    assert validate_file_plan_ownership(plan, fp).valid, "挂靠后闸必过"


def test_orphan_attach_no_candidate_fail_open():
    """无同模块候选：不猜挂，如实回 left（收尾器语义=留 VALIDATE 打回）。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    attached, left = attach_orphan_file_plan_entries(
        plan, ["mod-a/A.java", "mod-b/C.java"])
    assert attached == 0 and left == ["mod-b/C.java"]


def test_orphan_attach_prefers_deepest_prefix():
    plan = TaskPlan(subtasks=[
        _st("st-shallow", create=["mod-a/pom-notes.md"]),
        _st("st-deep", create=["mod-a/src/main/java/com/x/A.java"]),
    ], parallel_groups=[["st-shallow", "st-deep"]])
    attached, left = attach_orphan_file_plan_entries(
        plan, ["mod-a/src/main/java/com/x/B.java"])
    assert attached == 1 and not left
    assert ("mod-a/src/main/java/com/x/B.java"
            in plan.subtasks[1].scope.create_files), "共享前缀最深者优先"


def test_orphan_attach_idempotent():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/sub/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    fp = ["mod-a/A.java", "mod-a/sub/B.java", "mod-a/sub/C.java"]
    a1, _ = attach_orphan_file_plan_entries(plan, fp)
    a2, _ = attach_orphan_file_plan_entries(plan, fp)
    assert a1 == 1 and a2 == 0, "二次调用零变更（幂等）"
    assert plan.subtasks[1].scope.create_files.count("mod-a/sub/C.java") == 1


# ── ② 外科通道 strict 语义回归（重构后不回归 round40 行为）──

def test_fileplan_repair_strict_still_bails_on_unattachable():
    prior = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    state = {
        "plan": prior,
        "plan_validation_feedback": "file_plan 文件无 owner 子任务: mod-b/C.java",
        "plan_validation_issues": ["file_plan 文件无 owner 子任务: mod-b/C.java"],
        "tech_design_file_plan": ["mod-a/A.java", "mod-a/B.java", "mod-b/C.java"],
    }
    assert maybe_file_plan_repair(state) is None, "挂不上必须整体回退全量重拆（不半修）"


def test_fileplan_repair_strict_repairs_when_attachable():
    prior = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/sub/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    state = {
        "plan": prior,
        "plan_validation_feedback": "file_plan 文件无 owner 子任务: mod-a/sub/C.java",
        "plan_validation_issues": ["file_plan 文件无 owner 子任务: mod-a/sub/C.java"],
        "tech_design_file_plan": ["mod-a/A.java", "mod-a/sub/B.java", "mod-a/sub/C.java"],
    }
    repaired = maybe_file_plan_repair(state)
    assert repaired is not None
    assert "mod-a/sub/C.java" in repaired.subtasks[1].scope.create_files
    assert prior.subtasks[1].scope.create_files == ["mod-a/sub/B.java"], \
        "deepcopy：绝不半改原 plan"


# ── ③ 收尾器：组合修复（P1 抢跑后的 plan 也能被修）──

def test_finisher_attaches_orphans_regardless_of_plan_source():
    """互斥病根治本：收尾器不看 plan 从哪来，孤儿一律确定性挂靠。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["alarm-task/src/A.java"]),
        _st("st-sql", create=["sql/a.sql"]),
    ], parallel_groups=[["st-1", "st-sql"]])
    fp = ["alarm-task/src/A.java", "sql/a.sql", "sql/orphan.sql"]
    out = finish_plan_deterministic(plan, fp)
    assert out["orphans_attached"] == 1 and not out["orphans_left"]
    assert validate_file_plan_ownership(plan, fp).valid


def test_finisher_injects_scaffolds_for_unclaimed_deps():
    """R41-2：脚手架注入不再依赖符号外科存活——收尾器直接注入。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java", "mod-a/pom.xml"]),
        _st("st-c", create=["mod-c/src/C.java", "mod-c/pom.xml"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-c", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-b", "artifacts": ["org.x:mod-a"]},
    ]}
    out = finish_plan_deterministic(
        plan, ["mod-a/src/A.java", "mod-c/src/C.java", "mod-b/src/B.java"])
    assert out["scaffolds"] == ["mod-b"]
    sids = {st.id for st in plan.subtasks}
    assert "st-scaffold-mod-b" in sids
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-mod-b")
    # F5：无 project_path 时保守 MODIFY（writable）——绝不 CREATE 盖基线 pom
    assert "mod-b/pom.xml" in (list(scaffold.scope.writable)
                               + list(scaffold.scope.create_files))
    # bootstrap 已执行的证据：推断 harness 至少带命令白名单（语言推断依赖真实任务
    # 描述，最小测试描述推不出 build_command 属 _infer_harness 正常保守行为）
    assert scaffold.harness is not None and scaffold.harness.extra_whitelist, \
        "收尾器自行 bootstrap harness（错过主循环）"
    assert scaffold.est_context_tokens > 0
    st2 = next(st for st in plan.subtasks if st.id == "st-2")
    assert "st-scaffold-mod-b" in st2.depends_on, "同模块写码子任务依赖脚手架"


def test_finisher_single_subtask_plan_skips_orphan_attach():
    """SIMPLE 面自证：单子任务计划收尾器不越权挂靠（与闸同口径跳过）。"""
    plan = TaskPlan(subtasks=[_st("st-1", create=["mod-a/A.java"])],
                    parallel_groups=[["st-1"]])
    out = finish_plan_deterministic(plan, ["mod-a/A.java", "mod-a/B.java"])
    assert out["orphans_attached"] == 0 and not out["orphans_left"]
    assert plan.subtasks[0].scope.create_files == ["mod-a/A.java"]


def test_finisher_fail_open_on_none_plan():
    out = finish_plan_deterministic(None, ["a/b.java"])
    assert out == {"scaffolds": [], "orphans_attached": 0, "orphans_left": []}


# ── ④ 对抗复核整改回归（F1/F2/F3/F5）──

def test_f3_attach_injects_intent_into_description_and_ac():
    """F3：挂靠必须带意图——worker prompt 提及该文件 + 验收标准兜底。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["sql/a.sql"]),
    ], parallel_groups=[["st-1", "st-2"]])
    attach_orphan_file_plan_entries(plan, ["sql/orphan.sql"])
    st2 = plan.subtasks[1]
    assert "sql/orphan.sql" in (st2.description or "")
    assert any("sql/orphan.sql" in c for c in (st2.acceptance_criteria or []))


def test_f1_attach_recorded_and_covers_merge_survives_scope_drift():
    """F1：挂靠记录进 plan.finisher_attached；#6 覆盖单调化跨轮键漂移仍能配对并回 covers。"""
    from swarm.brain.nodes import _merge_prior_covers_by_scope

    # 挂靠轮（prior）：st-2 被收尾器挂了 orphan.sql 并记录
    prior = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["sql/a.sql"]),
    ], parallel_groups=[["st-1", "st-2"]])
    attach_orphan_file_plan_entries(prior, ["sql/orphan.sql"])
    assert prior.finisher_attached == {"st-2": ["sql/orphan.sql"]}
    prior.subtasks[1].covers = ["req-1"]

    # 全量重拆轮（new）：LLM 原始 scope 不带 orphan.sql（键漂移场景）
    new = TaskPlan(subtasks=[
        _st("st-x", create=["mod-a/A.java"]),
        _st("st-y", create=["sql/a.sql"]),
    ], parallel_groups=[["st-x", "st-y"]])
    injected = _merge_prior_covers_by_scope(new, prior, {"req-1"})
    assert injected.get("st-y") == {"req-1"}, \
        "剔除挂靠记录后 scope 身份还原，covers 必须并回（不再静默丢失）"
    assert "req-1" in (new.subtasks[1].covers or [])


def test_f1_surgical_deepcopy_side_still_matches():
    """F1 对称性：外科 deepcopy 路径两侧都带挂靠文件+记录 → 剔除后键仍相等。"""
    from swarm.brain.nodes import _merge_prior_covers_by_scope
    prior = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["sql/a.sql"]),
    ], parallel_groups=[["st-1", "st-2"]])
    attach_orphan_file_plan_entries(prior, ["sql/orphan.sql"])
    prior.subtasks[1].covers = ["req-1"]
    copied = prior.model_copy(deep=True)  # 外科通道语义
    for st in copied.subtasks:
        st.covers = []
    injected = _merge_prior_covers_by_scope(copied, prior, {"req-1"})
    assert injected.get("st-2") == {"req-1"}


def test_f2_ownership_denominator_excludes_unrequested_tests():
    """F2：任务未要求测试时，测试路径不进归属分母（防挂靠→剥离→打回确定性弹跳）。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    fp = ["mod-a/A.java", "mod-a/B.java",
          "mod-a/src/test/java/ATest.java"]
    assert not validate_file_plan_ownership(plan, fp).valid, "不排除时按旧口径打回"
    assert validate_file_plan_ownership(plan, fp, exclude_test_paths=True).valid


def test_f2_finisher_skips_test_orphans():
    """F2：收尾器在 strip 之后运行，绝不把刚被剥掉的测试文件挂回去。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    fp = ["mod-a/A.java", "mod-a/B.java", "mod-a/src/test/java/ATest.java"]
    out = finish_plan_deterministic(plan, fp, task_description="加一个接口")
    assert out["orphans_attached"] == 0 and not out["orphans_left"]
    all_files = [f for st in plan.subtasks for f in st.scope.create_files]
    assert "mod-a/src/test/java/ATest.java" not in all_files


def test_f5_inject_unknown_project_path_defaults_modify():
    """F5：project_path 未知时脚手架保守走 MODIFY（writable），绝不 CREATE 盖基线 pom。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java", "mod-a/pom.xml"]),
        _st("st-c", create=["mod-c/src/C.java", "mod-c/pom.xml"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-c", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-b", "artifacts": ["org.x:mod-a"]},
    ]}
    injected = inject_build_scaffold_subtasks(plan, None)
    assert injected and injected[0]["pom_exists"] is True
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-mod-b")
    assert scaffold.scope.writable == ["mod-b/pom.xml"]
    assert not scaffold.scope.create_files


def test_f5_owner_check_normalizes_dot_slash():
    """F5：'./mod/pom.xml' 写法的 owner 必须被识别（防重复注入→T3 降级空壳）。"""
    from swarm.brain.contract_utils import unclaimed_contract_deps
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["./mod-b/pom.xml", "mod-b/src/B.java"]),
        _st("st-2", create=["mod-a/pom.xml", "mod-a/src/A.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-b", "artifacts": ["org.x:mod-a"]},
    ]}
    assert unclaimed_contract_deps(plan) == [], "归一后 ./mod-b/pom.xml 是合法 owner"
