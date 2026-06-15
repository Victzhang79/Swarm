"""P1-1 回归测试：scope 归一（消除同文件写权分散 + 被依赖产物自动入域）。

复现 task 0f93f1fc：st-1-1 create_files 含 NumberUtilsTest.java，st-1-2 想改它却
无写权 → scope_guard 拦截 → empty_diff → 子任务失败。
"""
from __future__ import annotations

from swarm.brain.contract_utils import normalize_plan_scopes
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _sub(sid, *, writable=None, readable=None, create=None, deps=None):
    return SubTask(
        id=sid, description=f"t {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [], readable=readable or [], create_files=create or []),
        depends_on=deps or [],
    )


def test_same_file_write_perm_unique():
    """同一文件被两个子任务列为写目标 → 只首个保留写权，后者降级 readable。"""
    plan = TaskPlan(
        subtasks=[
            _sub("st-1-1", create=["NumberUtilsTest.java"], writable=["NumberUtils.java"]),
            # st-1-2 也想创建/改 NumberUtilsTest.java（重复写权 → 冲突源）
            _sub("st-1-2", create=["NumberUtilsTest.java"], writable=["StringUtils.java"]),
        ],
        parallel_groups=[],
    )
    changed = normalize_plan_scopes(plan)
    assert changed is True
    s2 = next(s for s in plan.subtasks if s.id == "st-1-2")
    # st-1-2 不再创建/写 NumberUtilsTest.java
    assert "NumberUtilsTest.java" not in s2.scope.create_files
    assert "NumberUtilsTest.java" not in s2.scope.writable
    # 但降级成 readable（仍能读到）
    assert "NumberUtilsTest.java" in s2.scope.readable
    # st-1-2 自己的 StringUtils.java 写权保留
    assert "StringUtils.java" in s2.scope.writable
    # 首个写者 st-1-1 不受影响
    s1 = next(s for s in plan.subtasks if s.id == "st-1-1")
    assert "NumberUtilsTest.java" in s1.scope.create_files
    print("  ✅ scope 归一: 同文件写权唯一（首个保留，后者降级 readable）")


def test_dependency_product_auto_readable():
    """st-2 depends_on st-1，st-1 产出 NumberUtils.java → 自动并入 st-2 的 readable。"""
    plan = TaskPlan(
        subtasks=[
            _sub("st-1", create=["NumberUtils.java"]),
            _sub("st-2", writable=["StringUtils.java"], deps=["st-1"]),
        ],
        parallel_groups=[],
    )
    changed = normalize_plan_scopes(plan)
    assert changed is True
    s2 = next(s for s in plan.subtasks if s.id == "st-2")
    assert "NumberUtils.java" in s2.scope.readable, "被依赖产物应自动入 readable"
    print("  ✅ scope 归一: 被依赖产物自动入下游 readable")


def test_no_change_when_clean():
    """无冲突无依赖产物 → 不改动，返回 False。"""
    plan = TaskPlan(
        subtasks=[_sub("st-1", writable=["a.java"]), _sub("st-2", writable=["b.java"])],
        parallel_groups=[],
    )
    assert normalize_plan_scopes(plan) is False
    print("  ✅ scope 归一: 干净计划不误改")


def test_task_0f93f1fc_scenario_end_to_end():
    """完整复现 task 0f93f1fc 的 scope 配置冲突，验证归一后 st-1-2 能改其依赖产物。"""
    plan = TaskPlan(
        subtasks=[
            # st-1-1: 建 NumberUtils + 其测试
            _sub("st-1-1", create=["NumberUtils.java", "NumberUtilsTest.java"]),
            # st-1-2: 改 StringUtils（委托 NumberUtils），依赖 st-1-1
            _sub("st-1-2", writable=["StringUtils.java"], deps=["st-1-1"]),
        ],
        parallel_groups=[],
    )
    normalize_plan_scopes(plan)
    s12 = next(s for s in plan.subtasks if s.id == "st-1-2")
    # st-1-2 能读到 st-1-1 产出的 NumberUtils.java（委托调用需要）
    assert "NumberUtils.java" in s12.scope.readable
    # st-1-2 自己的写权完整
    assert "StringUtils.java" in s12.scope.writable
    print("  ✅ scope 归一: task 0f93f1fc 场景 — 下游可读上游产物，写权不冲突")


if __name__ == "__main__":
    tests = [
        test_same_file_write_perm_unique,
        test_dependency_product_auto_readable,
        test_no_change_when_clean,
        test_task_0f93f1fc_scenario_end_to_end,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n=== P1-1 scope 归一: {passed}/{passed+failed} passed ===")
    import sys
    sys.exit(1 if failed else 0)
