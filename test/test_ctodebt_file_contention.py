"""治本"文件被争抢"这一类(2026-06-18)：normalize_plan_scopes 仓库感知分流。

不止 pom：任何【已存在的聚合/注册类共享文件】(父 pom 的 <modules>、settings.gradle、
路由 index、DI 注册表、i18n bundle…)被多个【独立】子任务写时，保留各自写权并【串行化】
(防静默丢贡献，MERGE 3-way/rebase + bootstrap 传播收口)；真·新建撞车仍首写者独占、
其余降级 readable。配 Maven 父 pom 单 owner backstop + parallel_groups 清理。
"""
from __future__ import annotations

import subprocess

from swarm.brain.contract_utils import normalize_plan_scopes
from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path, files: dict[str, str]) -> str:
    """建临时 git repo，files 里的文件提交进 HEAD（=已存在于基线）。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    for rel, content in files.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return str(proj)


def _st(sid, *, writable=None, create=None, readable=None, depends=None):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(
            writable=writable or [], create_files=create or [], readable=readable or [],
        ),
        harness=TaskHarness(language="java"),
        depends_on=depends or [],
    )


# ── 1. 已存在父 pom，3 个独立子任务都写 → 全部保留写权 + 串行成链，不静默丢 ──
def test_existing_parent_pom_serialized_not_dropped(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": "<modules></modules>\n",
                            "mod1/x": "", "mod2/x": "", "mod3/x": ""})
    sts = [_st(f"st-{i}", writable=["pom.xml"], create=[f"mod{i}/src/A.java"]) for i in (1, 2, 3)]
    plan = TaskPlan(subtasks=sts, parallel_groups=[["st-1", "st-2", "st-3"]])
    normalize_plan_scopes(plan, project_path=proj)

    for st in plan.subtasks:
        assert "pom.xml" in (st.scope.writable or []), f"{st.id} 丢失 pom 写权: {st.scope}"
        assert "pom.xml" not in (st.scope.readable or []), f"{st.id} 不应被降级 readable"
    # 非首写者被串行化（有依赖）
    assert plan.subtasks[1].depends_on, "st-2 应串行化(依赖前序写者)"
    assert plan.subtasks[2].depends_on, "st-3 应串行化(依赖前序写者)"
    # parallel_groups 被清空（避免 validator parallel-group 同写硬 fail）
    assert plan.parallel_groups == [], "串行化后 vestigial 的 parallel_groups 应清空"
    # validator 通过（聚合同写串行后是依赖序 → warn 非 fail）
    res = validate_plan_structure(plan)
    assert res.valid, f"串行化后应通过校验, issues={res.issues}"


# ── 2. 已存在路由/注册 index（非 pom 的聚合文件）多独立写者 → 串行不降级 ──
def test_existing_registry_index_serialized(tmp_path):
    proj = _repo(tmp_path, {"src/router/index.ts": "export const routes = []\n",
                            "a": "", "b": ""})
    sts = [_st("st-1", writable=["src/router/index.ts"], create=["src/views/A.vue"]),
           _st("st-2", writable=["src/router/index.ts"], create=["src/views/B.vue"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan, project_path=proj)
    assert "src/router/index.ts" in plan.subtasks[0].scope.writable
    assert "src/router/index.ts" in plan.subtasks[1].scope.writable, "聚合 index 不应被降级丢贡献"
    assert plan.subtasks[1].depends_on, "第二写者应串行化"


# ── 3. 全新文件（不在 repo）2 独立创建者 → 首建，其余降级 readable（今日行为不回归）──
def test_new_file_collision_still_demotes(tmp_path):
    proj = _repo(tmp_path, {"existing": ""})  # NewShared.java 不在 repo
    sts = [_st("st-1", create=["src/NewShared.java"]),
           _st("st-2", create=["src/NewShared.java"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan, project_path=proj)
    assert "src/NewShared.java" in plan.subtasks[0].scope.create_files, "首写者保留 create"
    assert "src/NewShared.java" not in (plan.subtasks[1].scope.create_files or [])
    assert "src/NewShared.java" in plan.subtasks[1].scope.readable, "新建撞车非首写者降级 readable"
    assert "st-1" in (plan.subtasks[1].depends_on or []), "降级者依赖首写者"


# ── 4. 已存在文件被误放 create_files（2 写者）→ 重分类 modify，保留 writable 串行 ──
def test_existing_file_in_create_reclassified_modify(tmp_path):
    proj = _repo(tmp_path, {"config/application.yml": "k: v\n", "a": ""})
    sts = [_st("st-1", create=["config/application.yml"]),
           _st("st-2", writable=["config/application.yml"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan, project_path=proj)
    assert "config/application.yml" not in (plan.subtasks[0].scope.create_files or []), \
        "已存在文件不应留在 create(实为 modify)"
    assert "config/application.yml" in plan.subtasks[0].scope.writable
    assert "config/application.yml" in plan.subtasks[1].scope.writable
    assert plan.subtasks[1].depends_on


# ── 5. 防环：写者间已有反向依赖时不产生环 ──
def test_serialize_cycle_guard(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": "<modules></modules>\n", "a": "", "b": ""})
    # st-1 已依赖 st-2（与写者序相反），两者都写已存在 pom
    sts = [_st("st-1", writable=["pom.xml"], depends=["st-2"]),
           _st("st-2", writable=["pom.xml"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan, project_path=proj)
    res = validate_plan_structure(plan)
    assert "循环依赖" not in " ".join(res.issues), f"不应成环: {res.issues}"
    # 仍保留各自写权（串行链协作）
    assert "pom.xml" in plan.subtasks[0].scope.writable
    assert "pom.xml" in plan.subtasks[1].scope.writable


# ── 6. project_path=None → 退化为今日 demote 行为（向后兼容）──
def test_none_project_path_backward_compat():
    sts = [_st("st-1", writable=["pom.xml"]), _st("st-2", writable=["pom.xml"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan, project_path=None)
    assert "pom.xml" not in (plan.subtasks[1].scope.writable or []), "无 project_path 应退化 demote"
    assert "pom.xml" in (plan.subtasks[1].scope.readable or [])


# ── 7. Maven 父 pom 单 owner backstop：有新模块但无人 own 根 pom → 指派 owner ──
def test_maven_parent_pom_owner_backstop(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": "<modules></modules>\n"})
    sts = [_st("st-1", create=["alarm-app/pom.xml", "alarm-app/src/A.java"]),
           _st("st-2", create=["alarm-api/pom.xml", "alarm-api/src/B.java"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan, project_path=proj)
    owners = [st.id for st in plan.subtasks if "pom.xml" in (st.scope.writable or [])]
    assert owners == ["st-1"], f"应指派单一 owner(首个建模块 pom 的子任务), 实际 {owners}"
    ac = " ".join(plan.subtasks[0].acceptance_criteria or [])
    assert "alarm-app" in ac and "alarm-api" in ac, f"owner 应登记全部新模块: {ac}"
    assert "st-1" in (plan.subtasks[1].depends_on or []), "另一模块子任务依赖 owner"


# ── 8. backstop 不动既有 owner：已有人 own 根 pom 时不重复指派 ──
def test_maven_backstop_noop_when_owner_exists(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": "<modules></modules>\n"})
    sts = [_st("st-0", writable=["pom.xml"], create=["alarm-app/pom.xml"]),
           _st("st-1", create=["alarm-api/pom.xml", "alarm-api/src/B.java"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan, project_path=proj)
    owners = [st.id for st in plan.subtasks if "pom.xml" in (st.scope.writable or [])]
    assert owners == ["st-0"], f"已有 owner 时 backstop 不应再指派, 实际 {owners}"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
