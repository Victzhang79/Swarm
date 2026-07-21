"""DR-01-F2(#46) 治本红测试：validate_plan_structure 的写冲突/根聚合清单闸必须用【归一】
路径键，否则同一文件的路径形态变体（'./pom.xml' vs 'pom.xml'）逃过所有冲突判定。

修复前：writable_map/seen/根聚合成员判定用 scope 原始串作键 → 变体视作不同文件 → 不触发
硬失败；且 './pom.xml' 不在裸 _ROOT_AGGREGATOR_MANIFESTS → 双写者 backstop 被绕过。
修复后：三处统一走 _norm_scope_path 归一 → 变体被识别为同一文件 → 硬失败命中。
"""
from __future__ import annotations

from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


def _st(sid, *, writable=None, create=None, depends=None):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(writable=writable or [], create_files=create or [], readable=[]),
        harness=TaskHarness(language="java"),
        depends_on=depends or [],
    )


def _has_issue(res, *needles):
    return any(all(n in msg for n in needles) for msg in res.issues)


def test_prefix_variant_cross_subtask_write_conflict_detected():
    # 两个【无依赖】子任务对同一文件用不同前缀写 → 归一后=同一文件 → 并行必冲突硬失败。
    st_a = _st("st-a", writable=["./ruoyi-alarm/X.java"])
    st_b = _st("st-b", writable=["ruoyi-alarm/X.java"])
    res = validate_plan_structure(TaskPlan(subtasks=[st_a, st_b]))
    assert not res.valid, "路径前缀变体的跨子任务写冲突未被识别（#46 fail-open）"
    assert _has_issue(res, "同时写"), f"缺写冲突 issue: {res.issues}"


def test_prefix_variant_root_aggregator_backstop_not_bypassed():
    # 两个无依赖子任务分别写 './pom.xml' 与 'pom.xml' → 归一后都是根聚合清单 → 单写者 backstop。
    st_a = _st("st-a", writable=["./pom.xml"])
    st_b = _st("st-b", writable=["pom.xml"])
    res = validate_plan_structure(TaskPlan(subtasks=[st_a, st_b]))
    assert not res.valid
    assert _has_issue(res, "根聚合清单"), f"根聚合双写者 backstop 被 './' 绕过: {res.issues}"


def test_parallel_group_prefix_variant_conflict_detected():
    # 同一并行组内两子任务用不同前缀写同文件 → 归一后并行冲突命中。
    st_a = _st("st-a", writable=["./mod/A.java"])
    st_b = _st("st-b", writable=["mod/A.java"])
    plan = TaskPlan(subtasks=[st_a, st_b], parallel_groups=[["st-a", "st-b"]])
    res = validate_plan_structure(plan)
    assert not res.valid
    assert _has_issue(res, "并行冲突") or _has_issue(res, "同时写"), f"{res.issues}"


def test_legit_distinct_files_still_pass():
    # 归一收紧不得误伤：不同模块的不同文件仍放行。
    st_a = _st("st-a", writable=["ruoyi-alarm/A.java"])
    st_b = _st("st-b", writable=["ruoyi-admin/B.java"])
    res = validate_plan_structure(TaskPlan(subtasks=[st_a, st_b]))
    assert res.valid, f"误伤合法不同文件: {res.issues}"
