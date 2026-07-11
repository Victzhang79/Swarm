#!/usr/bin/env python3
"""R39-4（round39 治本批）—— 规则5 pom owner 落空 → 确定性脚手架子任务注入。

取证：round39 三轮 VALIDATE 各 6 模块规则5 WARNING（55 artifacts 落空）无人消费
（#30② 同病）；脚手架目前只靠 prompts.py:77-78 叮嘱 LLM，LLM 没听=落空。
治本（零 LLM）：unclaimed_contract_deps 命中的模块，确定性注入"建/补该模块构建
文件"的脚手架子任务——契约 dependencies 全集随子任务 contract 落地（写代码的
子任务碰不到构建文件，缺一个依赖=整模块编译失败）；同模块其余子任务 depends_on
脚手架（先有构建文件再编译）。构建文件路径沿用规则5 自身口径（<module>/pom.xml，
Maven 专属是既有产品决策，round24 A2 先例）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import (  # noqa: E402
    inject_build_scaffold_subtasks,
    unclaimed_contract_deps,
)
from swarm.brain.plan_validator import validate_plan_structure  # noqa: E402
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


def _plan_two_modules():
    """round39 真场景缩影：两物理模块、零 pom owner（6 模块全落空的最小版）。

    注意规则5 的 A5 归并早退：恰好一个 pom owner 时视为单物理模块恒空——
    零 owner / ≥2 owner 才进落空判定。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/main/java/A.java"]),
        _st("st-2", create=["mod-b/src/main/java/B.java"]),
        _st("st-3", create=["mod-b/src/main/java/C.java"]),
    ], parallel_groups=[["st-1"], ["st-2", "st-3"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-a", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "mod-b", "artifacts": ["org.projectlombok:lombok",
                                          "org.springframework:spring-context"]},
    ]}
    return plan


def test_inject_creates_scaffold_for_unclaimed_module():
    plan = _plan_two_modules()
    assert len(unclaimed_contract_deps(plan)) == 2, "前置：两模块规则5 全落空"
    injected = inject_build_scaffold_subtasks(plan)
    assert {e["module"] for e in injected} == {"mod-a", "mod-b"}
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    sc_st = next(st for st in plan.subtasks if st.id == sid)
    assert "mod-b/pom.xml" in sc_st.scope.create_files, "基线无 pom → create_files"
    assert sc_st.contract.get("dependencies"), "契约 dependencies 全集随脚手架落地"
    arts = sc_st.contract["dependencies"][0]["artifacts"]
    assert "org.springframework:spring-context" in arts
    # 注入后规则5 清零（治的就是"WARNING 无人消费"）
    assert not unclaimed_contract_deps(plan)


def test_module_subtasks_depend_on_scaffold():
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan)
    sid_b = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    st2 = next(st for st in plan.subtasks if st.id == "st-2")
    st3 = next(st for st in plan.subtasks if st.id == "st-3")
    assert sid_b in st2.depends_on and sid_b in st3.depends_on, (
        "同模块子任务先等构建文件落地再编译")
    st1 = next(st for st in plan.subtasks if st.id == "st-1")
    assert sid_b not in st1.depends_on, "别的模块不受影响（不过度串行）"
    sc_st = next(st for st in plan.subtasks if st.id == sid_b)
    assert not sc_st.depends_on, "脚手架无上游依赖=不可能成环"


def test_plan_structure_stays_valid_after_inject():
    plan = _plan_two_modules()
    inject_build_scaffold_subtasks(plan)
    r = validate_plan_structure(plan)
    assert r.valid, f"注入后结构校验必须通过（parallel_groups 完整性等）: {r.issues}"


def test_existing_pom_goes_writable(tmp_path):
    proj = tmp_path / "proj"
    (proj / "mod-b").mkdir(parents=True)
    (proj / "mod-b/pom.xml").write_text("<project/>", encoding="utf-8")
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan, project_path=str(proj))
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    sc_st = next(st for st in plan.subtasks if st.id == sid)
    assert "mod-b/pom.xml" in sc_st.scope.writable, "基线已有 pom → writable 修改"
    assert "mod-b/pom.xml" not in sc_st.scope.create_files


def test_idempotent_and_noop_when_clean():
    plan = _plan_two_modules()
    inject_build_scaffold_subtasks(plan)
    n = len(plan.subtasks)
    assert inject_build_scaffold_subtasks(plan) == [], "二次注入无事可做"
    assert len(plan.subtasks) == n
    # 单 pom owner 场景（A5 归并：规则5 恒空）→ 不注入
    clean = TaskPlan(subtasks=[
        _st("st-1", create=["mod-x/pom.xml", "mod-x/src/A.java"])],
        parallel_groups=[["st-1"]])
    clean.shared_contract = {"dependencies": [
        {"module": "mod-x", "artifacts": ["g:a"]}]}
    assert unclaimed_contract_deps(clean) == [], "前置：A5 归并恒空"
    assert inject_build_scaffold_subtasks(clean) == []
