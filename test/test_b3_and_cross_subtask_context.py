"""B3 + 跨子任务上下文传递回归。

B3: plan_validator 检测依赖序子任务文件重叠（warn）+ create_files 纳入写冲突检测。
ctx: _inject_predecessor_context 把前序产出的方法签名注入后序子任务 context_snippets。
"""
from swarm.brain.nodes.dispatch import _inject_predecessor_context
from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput


def _st(sid, writable=None, create=None, depends_on=None):
    return SubTask(
        id=sid, description=f"task {sid}",
        scope=FileScope(writable=writable or [], create_files=create or []),
        depends_on=depends_on or [],
    )


# ── B3: 文件重叠检测 ──
def test_dependent_subtasks_overlap_warns():
    """依赖序子任务写同文件 → warn（不阻断）。"""
    plan = TaskPlan(
        subtasks=[_st("a", writable=["X.java"]), _st("b", writable=["X.java"], depends_on=["a"])],
        parallel_groups=[["a"], ["b"]],
    )
    r = validate_plan_structure(plan)
    assert r.valid, "依赖序重叠应仅 warn 不阻断"
    assert any("文件不重叠" in w or "MERGE 可能冲突" in w for w in r.warnings)


def test_independent_subtasks_overlap_fails():
    """无依赖子任务写同文件 → 硬失败。"""
    plan = TaskPlan(
        subtasks=[_st("a", writable=["X.java"]), _st("b", writable=["X.java"])],
        parallel_groups=[["a", "b"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid
    assert any("同时写" in i for i in r.issues)


def test_create_files_counted_in_overlap():
    """create_files 也纳入写冲突检测。"""
    plan = TaskPlan(
        subtasks=[_st("a", create=["New.java"]), _st("b", create=["New.java"])],
        parallel_groups=[["a", "b"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid


# ── 跨子任务上下文传递 ──
def test_inject_predecessor_signatures():
    """前序产出的方法签名注入后序子任务 context_snippets。"""
    st_a = _st("a", writable=["IService.java"])
    st_b = _st("b", writable=["ServiceImpl.java"], depends_on=["a"])
    plan = TaskPlan(subtasks=[st_a, st_b], parallel_groups=[["a"], ["b"]])
    results = {
        "a": WorkerOutput(
            subtask_id="a",
            diff="--- a/IService.java\n+++ b/IService.java\n@@ -1 +1,3 @@\n+public interface IService {\n+    List<User> selectUsers(User u);\n+}",
            summary="接口", l1_passed=True,
        )
    }
    _inject_predecessor_context([st_b], plan, results)
    snip = st_b.context_snippets
    assert "前序子任务已产出" in snip
    assert "selectUsers" in snip or "IService" in snip


def test_no_deps_no_injection():
    """无依赖子任务不注入。"""
    st = _st("a", writable=["X.java"])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["a"]])
    _inject_predecessor_context([st], plan, {})
    assert st.context_snippets == ""


def test_injection_idempotent():
    """重复注入不叠加（幂等）。"""
    st_b = _st("b", writable=["Impl.java"], depends_on=["a"])
    plan = TaskPlan(subtasks=[_st("a", writable=["I.java"]), st_b], parallel_groups=[["a"], ["b"]])
    results = {"a": WorkerOutput(subtask_id="a",
               diff="+++ b/I.java\n+public void foo();", summary="x", l1_passed=True)}
    _inject_predecessor_context([st_b], plan, results)
    first = st_b.context_snippets
    _inject_predecessor_context([st_b], plan, results)
    assert st_b.context_snippets == first, "重复注入应幂等"
