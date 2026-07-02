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
    assert "ruoyi-alarm-app/pom.xml" in sc.create_files, "模块自己的 pom 必须并入写权"
    # 回滚后：Rule 3 不再碰根 pom（父注册交给脚手架子任务 + bootstrap 传播）
    assert "pom.xml" not in (sc.writable or []), "Rule 3 不应再添加根 pom"


def test_acceptance_criteria_also_detected():
    """`-pl` 出现在 acceptance_criteria 里同样应触发模块 pom 补全（不碰根 pom）。"""
    plan = _plan(
        FileScope(create_files=["mymod/src/main/java/A.java"]),
        TaskHarness(language="java"),
    )
    plan.subtasks[0].acceptance_criteria = ["mvn -pl mymod -am compile"]
    normalize_plan_scopes(plan)
    sc = plan.subtasks[0].scope
    assert "mymod/pom.xml" in sc.create_files
    assert "pom.xml" not in (sc.writable or [])


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
        ),
        TaskHarness(language="java", build_command="mvn -pl mod -am compile"),
    )
    normalize_plan_scopes(plan)
    sc = plan.subtasks[0].scope
    assert sc.create_files.count("mod/pom.xml") == 1


# ── 回滚守护：多 builder 时 Rule 3 不再给任何子任务加根 pom（不喷洒、不单归属） ──
def test_multi_module_rule3_does_not_touch_root_pom():
    sts = []
    for i, mod in enumerate(["alarm-app", "alarm-channel", "alarm-api"], start=1):
        sts.append(SubTask(
            id=f"st-{i}", description="d",
            scope=FileScope(create_files=[f"{mod}/src/main/java/X{i}.java"]),
            harness=TaskHarness(language="java", build_command=f"mvn -pl {mod} -am compile"),
        ))
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan)
    root_writers = [s.id for s in plan.subtasks
                    if "pom.xml" in (s.scope.writable or []) or "pom.xml" in (s.scope.create_files or [])]
    assert root_writers == [], f"Rule 3 不应给任何子任务加根 pom，实际 {root_writers}"
    # 但各模块自己的 pom 仍补齐（不同文件，无争用）
    for i, mod in enumerate(["alarm-app", "alarm-channel", "alarm-api"], start=1):
        assert f"{mod}/pom.xml" in plan.subtasks[i - 1].scope.create_files


# ── 规则5：模块依赖契约落地（治本 task f9e38dae：编译期缺依赖→必败→全量 replan） ──
def test_rule5_deps_appended_to_pom_owner_acceptance():
    """shared_contract.dependencies 的 artifacts 应确定性追加进【模块 pom owner】验收。"""
    owner = SubTask(
        id="st-1", description="脚手架+AlarmApp",
        scope=FileScope(create_files=["ruoyi-alarm/pom.xml", "ruoyi-alarm/src/main/java/App.java"]),
    )
    coder = SubTask(
        id="st-24", description="VoipNotifyServiceImpl 用 RedisTemplate",
        scope=FileScope(create_files=["ruoyi-alarm/src/main/java/impl/Voip.java"]),
        depends_on=["st-1"],
    )
    plan = TaskPlan(
        subtasks=[owner, coder],
        shared_contract={
            "dependencies": [
                {"module": "ruoyi-alarm",
                 "artifacts": ["org.projectlombok:lombok", "org.springframework.boot:spring-boot-starter-data-redis"],
                 "reason": "引擎/渠道子任务用 @Slf4j/RedisTemplate"}
            ]
        },
    )
    changed = normalize_plan_scopes(plan)
    assert changed is True
    ac = plan.subtasks[0].acceptance_criteria or []
    hit = [c for c in ac if "ruoyi-alarm/pom.xml 必须声明依赖" in c]
    assert hit, f"pom owner 验收应含依赖声明，实际 {ac}"
    assert "org.projectlombok:lombok" in hit[0]
    assert "spring-boot-starter-data-redis" in hit[0]
    # 非 owner 子任务不应被加依赖验收
    assert not any("必须声明依赖" in c for c in (plan.subtasks[1].acceptance_criteria or []))


def test_rule5_no_owner_logs_warning_no_crash():
    """依赖契约指向的模块无 pom owner 时：告警但不崩、不误伤。

    用 patch 直接拦 logger.warning（caplog 在全量套件里受其他测试改 logging 配置影响而不稳）。
    """
    from unittest.mock import patch
    coder = SubTask(
        id="st-1", description="只写代码没人建 pom",
        scope=FileScope(create_files=["orphan-mod/src/main/java/A.java"]),
    )
    plan = TaskPlan(
        subtasks=[coder],
        shared_contract={"dependencies": [{"module": "orphan-mod", "artifacts": ["g:a"]}]},
    )
    with patch("swarm.brain.contract_utils.logger.warning") as mock_warn:
        normalize_plan_scopes(plan)
    assert any("无 pom owner 承接" in str(c.args[0]) for c in mock_warn.call_args_list), \
        mock_warn.call_args_list
    assert not any("必须声明依赖" in c for c in (plan.subtasks[0].acceptance_criteria or []))


def test_rule5_idempotent_and_dedup():
    """重复跑 normalize 不重复追加同一条依赖验收。"""
    owner = SubTask(id="st-1", description="脚手架",
                    scope=FileScope(create_files=["m/pom.xml"]))
    plan = TaskPlan(subtasks=[owner],
                    shared_contract={"dependencies": [{"module": "m", "artifacts": ["g:a"]}]})
    normalize_plan_scopes(plan)
    normalize_plan_scopes(plan)
    notes = [c for c in (plan.subtasks[0].acceptance_criteria or []) if "必须声明依赖" in c]
    assert len(notes) == 1, f"应去重为 1 条，实际 {notes}"


def test_rule5_noop_without_dependencies():
    """无 shared_contract.dependencies 时规则5 不动任何东西（向后兼容）。"""
    owner = SubTask(id="st-1", description="脚手架",
                    scope=FileScope(create_files=["m/pom.xml", "m/src/A.java"]))
    plan = TaskPlan(subtasks=[owner])  # 无 shared_contract
    normalize_plan_scopes(plan)
    assert not any("必须声明依赖" in c for c in (plan.subtasks[0].acceptance_criteria or []))


# ── 验证器：N 子任务写同一文件聚合成 1 条（不再 O(n²) 刷屏） ──
def test_validator_aggregates_conflicts():
    from swarm.brain.plan_validator import validate_plan_structure

    # 5 个子任务都写根 pom.xml → D1 backstop：硬失败且聚合成 1 条（不 O(n²) 刷屏）
    sts = [SubTask(id=f"st-{i}", description="d",
                   scope=FileScope(writable=["pom.xml"])) for i in range(1, 6)]
    res = validate_plan_structure(TaskPlan(subtasks=sts))
    pom_msgs = [m for m in res.issues if "pom.xml" in m]
    assert len(pom_msgs) == 1, f"应聚合成 1 条，实际 {len(pom_msgs)}: {pom_msgs}"
    assert "5 个写者" in pom_msgs[0] and "唯一 aggregator-owner" in pom_msgs[0]


# ── 验证器：同一子任务文件双列(writable+create_files) 不报自冲突(st-N 与 st-N) ──
def test_validator_no_self_conflict_on_dual_listed_file():
    from swarm.brain.plan_validator import validate_plan_structure

    st = SubTask(id="st-1", description="d",
                 scope=FileScope(writable=["a.txt"], create_files=["a.txt"]))
    res = validate_plan_structure(TaskPlan(subtasks=[st]))
    self_confs = [m for m in (res.issues + res.warnings) if "st-1 与 st-1" in m]
    assert not self_confs, f"不应出现自比较冲突: {self_confs}"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
