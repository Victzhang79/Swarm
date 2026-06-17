"""治本 task 69d34b1b：Maven 新模块构建闸门 scope 可满足性补全（normalize_plan_scopes 规则3）。

现场：子任务新建 `<module>/src/...` 但模块 pom + 父 pom 注册不在 scope → `mvn -pl <module>` 必败、
worker 够不着。规则3 自动把 `<module>/pom.xml` + 根 `pom.xml` 并入写权。
"""
from __future__ import annotations

from swarm.brain.contract_utils import normalize_plan_scopes
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


def _plan(scope: FileScope, harness: TaskHarness) -> TaskPlan:
    return TaskPlan(subtasks=[SubTask(id="st-2", description="d", scope=scope, harness=harness)])


def test_new_maven_module_injects_pom_and_parent():
    plan = _plan(
        FileScope(create_files=["ruoyi-alarm-app/src/main/java/com/x/AlarmApp.java"]),
        TaskHarness(language="java", build_command="mvn -pl ruoyi-alarm-app -am -q compile"),
    )
    changed = normalize_plan_scopes(plan)
    sc = plan.subtasks[0].scope
    assert changed is True
    assert "ruoyi-alarm-app/pom.xml" in sc.create_files, "模块 pom 必须并入写权"
    assert "pom.xml" in sc.writable, "根 pom 必须并入写权（注册 <module>）"


def test_acceptance_criteria_also_detected():
    """`-pl` 出现在 acceptance_criteria 里同样应触发补全。"""
    plan = _plan(
        FileScope(create_files=["mymod/src/main/java/A.java"]),
        TaskHarness(language="java"),
    )
    plan.subtasks[0].acceptance_criteria = ["mvn -pl mymod -am compile"]
    normalize_plan_scopes(plan)
    sc = plan.subtasks[0].scope
    assert "mymod/pom.xml" in sc.create_files
    assert "pom.xml" in sc.writable


def test_existing_module_not_expanded():
    """改既有模块（模块目录下无 create_files）不应被补 pom（避免乱扩 scope）。"""
    plan = _plan(
        FileScope(writable=["existing-mod/src/main/java/B.java"]),
        TaskHarness(language="java", build_command="mvn -pl existing-mod -am -q compile"),
    )
    changed = normalize_plan_scopes(plan)
    sc = plan.subtasks[0].scope
    assert "existing-mod/pom.xml" not in sc.create_files
    assert "pom.xml" not in (sc.writable or [])
    # 该用例不应仅因规则3 而判 changed
    assert changed is False


def test_artifactid_form_skipped():
    """`-pl :artifactId` 形式无法可靠映射目录 → 跳过，不补 pom。"""
    plan = _plan(
        FileScope(create_files=["somedir/src/main/java/C.java"]),
        TaskHarness(language="java", build_command="mvn -pl :alarm-app -am compile"),
    )
    normalize_plan_scopes(plan)
    sc = plan.subtasks[0].scope
    assert not any(p.endswith("pom.xml") for p in (sc.create_files or [])), sc.create_files
    assert "pom.xml" not in (sc.writable or [])


def test_pom_not_duplicated_when_already_in_scope():
    """模块 pom 已在某处写权时不重复添加。"""
    plan = _plan(
        FileScope(
            create_files=["mod/src/main/java/D.java", "mod/pom.xml"],
            writable=["pom.xml"],
        ),
        TaskHarness(language="java", build_command="mvn -pl mod -am compile"),
    )
    normalize_plan_scopes(plan)
    sc = plan.subtasks[0].scope
    assert sc.create_files.count("mod/pom.xml") == 1
    assert sc.writable.count("pom.xml") == 1


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
