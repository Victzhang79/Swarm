"""回归：scope 归一消除并发同文件写冲突（Bug-3，task 0f93f1fc 实证）。

两个子任务都对同一文件有写权（create_files/writable）时：
1. 写权唯一化——只有首写者保留写权，后续降级为 readable；
2. 降级者获得指向首写者的 depends_on，强制串行，杜绝并行执行时的运行期物理冲突。
"""

from __future__ import annotations

from swarm.brain.contract_utils import normalize_plan_scopes
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _mk(sid, *, create=None, writable=None, readable=None, deps=None):
    return SubTask(
        id=sid,
        description=f"sub {sid}",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(
            create_files=create or [],
            writable=writable or [],
            readable=readable or [],
        ),
        depends_on=deps or [],
        acceptance_criteria=["ok"],
    )


def test_same_file_write_uniqueness():
    # st-1 与 st-2 都 create 同一文件 → 只 st-1 保留写权，st-2 降级
    f = "ruoyi-common/src/test/java/.../NumberUtilsTest.java"
    plan = TaskPlan(
        subtasks=[_mk("st-1", create=[f]), _mk("st-2", create=[f])],
        parallel_groups=[], shared_contract={},
    )
    changed = normalize_plan_scopes(plan)
    assert changed
    s1 = next(s for s in plan.subtasks if s.id == "st-1")
    s2 = next(s for s in plan.subtasks if s.id == "st-2")
    assert f in (s1.scope.create_files), "首写者保留写权"
    assert f not in (s2.scope.create_files or []), "非首写者写权被移除"
    assert f in s2.scope.readable, "非首写者降级为 readable"


def test_demoted_writer_gets_dependency_on_first_writer():
    # Bug-3 核心：st-2 降级后必须依赖 st-1，强制串行
    f = "x/NumberUtilsTest.java"
    plan = TaskPlan(
        subtasks=[_mk("st-1", create=[f]), _mk("st-2", create=[f])],
        parallel_groups=[], shared_contract={},
    )
    normalize_plan_scopes(plan)
    s2 = next(s for s in plan.subtasks if s.id == "st-2")
    assert "st-1" in s2.depends_on, f"降级者应依赖首写者，实际 depends_on={s2.depends_on}"


def test_no_conflict_no_change():
    # 不同文件，无冲突 → 不改写权、不加依赖
    plan = TaskPlan(
        subtasks=[_mk("st-1", create=["a.java"]), _mk("st-2", create=["b.java"])],
        parallel_groups=[], shared_contract={},
    )
    normalize_plan_scopes(plan)
    s2 = next(s for s in plan.subtasks if s.id == "st-2")
    assert "a.java" in s2.scope.create_files or "b.java" in s2.scope.create_files
    assert "st-1" not in s2.depends_on


def test_three_writers_all_depend_on_first():
    f = "shared.java"
    plan = TaskPlan(
        subtasks=[_mk("st-1", writable=[f]), _mk("st-2", writable=[f]), _mk("st-3", writable=[f])],
        parallel_groups=[], shared_contract={},
    )
    normalize_plan_scopes(plan)
    for sid in ("st-2", "st-3"):
        s = next(x for x in plan.subtasks if x.id == sid)
        assert "st-1" in s.depends_on, f"{sid} 应依赖首写者 st-1"
        assert f not in (s.scope.writable or [])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== scope 归一防并发写冲突: {len(fns)}/{len(fns)} passed ===")
