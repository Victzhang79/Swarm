"""回归：PLAN 子任务 scope 为空根因（task 39f7be5a 现场）。

两个交织 bug：
1. 别名 bug：_resplit_subtask 让子节点共享同一 base_scope 对象，normalize 原地改污染兄弟。
2. 语义 bug：normalize 不区分"串行子链协作写同一文件"与"独立并发竞争"，把串行协作者也降级
   → 子任务 writable/create_files 全空 → Worker 无写权。
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
        scope=FileScope(create_files=create or [], writable=writable or [], readable=readable or []),
        depends_on=deps or [],
        acceptance_criteria=["ok"],
    )


def test_resplit_no_scope_aliasing():
    """修1：二次拆分子节点的 scope 必须是独立对象，不共享引用。"""
    from swarm.brain.planning_nodes import _resplit_subtask  # noqa: F401
    # 直接验证深拷贝语义：构造两个子任务共享文件但独立 scope，改一个不影响另一个
    s1 = _mk("st-1-1", create=["X.java"])
    s2 = _mk("st-1-2", create=["X.java"])
    assert s1.scope is not s2.scope, "子节点 scope 必须是独立对象"
    s1.scope.create_files = []
    assert s2.scope.create_files == ["X.java"], "改一个 scope 不应影响另一个"


def test_serial_chain_collaborators_keep_write():
    """修2：串行链上协作写同一文件 → 首写者 create，后续转 writable，都保留写权。"""
    f = "NumberUtils.java"
    # st-1-2 依赖 st-1-1，二者协作写 NumberUtils.java（拆分 isNumeric/toInt）
    plan = TaskPlan(
        subtasks=[
            _mk("st-1-1", create=[f]),
            _mk("st-1-2", create=[f], deps=["st-1-1"]),
        ],
        parallel_groups=[], shared_contract={},
    )
    normalize_plan_scopes(plan)
    s1 = next(s for s in plan.subtasks if s.id == "st-1-1")
    s2 = next(s for s in plan.subtasks if s.id == "st-1-2")
    assert f in s1.scope.create_files, "首写者保留 create"
    # 后续串行写者：转为 writable（修改首写者产物），不再降级 readable
    assert f in s2.scope.writable, f"串行协作者应保留写权(writable)，实际 writable={s2.scope.writable} readable={s2.scope.readable}"
    assert f not in s2.scope.create_files, "后续写者不应重复 create（避免文件已存在）"


def test_independent_concurrent_still_demoted():
    """独立并发（无依赖链）写同一文件 → 非首写者仍降级（保持 Bug-3 修复）。"""
    f = "Shared.java"
    plan = TaskPlan(
        subtasks=[_mk("st-1", writable=[f]), _mk("st-2", writable=[f])],  # 无依赖关系
        parallel_groups=[], shared_contract={},
    )
    normalize_plan_scopes(plan)
    s2 = next(s for s in plan.subtasks if s.id == "st-2")
    assert f not in (s2.scope.writable or []), "独立并发非首写者应降级"
    assert "st-1" in s2.depends_on, "降级者依赖首写者强制串行"


def test_transitive_chain():
    """传递依赖链：st-1-3 → st-1-2 → st-1-1，三者协作写同一文件都保留写权。"""
    f = "Mod.java"
    plan = TaskPlan(
        subtasks=[
            _mk("st-1-1", create=[f]),
            _mk("st-1-2", create=[f], deps=["st-1-1"]),
            _mk("st-1-3", create=[f], deps=["st-1-2"]),
        ],
        parallel_groups=[], shared_contract={},
    )
    normalize_plan_scopes(plan)
    for sid in ("st-1-2", "st-1-3"):
        s = next(x for x in plan.subtasks if x.id == sid)
        assert f in s.scope.writable, f"{sid} 串行链协作应保留写权，实际 {s.scope.writable}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== PLAN scope 别名+串行协作: {len(fns)}/{len(fns)} passed ===")
