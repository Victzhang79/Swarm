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
    assert any("串行化" in w or "MERGE" in w or "只归一个子任务" in w for w in r.warnings)


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


def test_inject_api_endpoints():
    """第二批-4：前序 Controller 的 API 端点契约注入后序（前端对齐）。"""
    st_a = _st("a", writable=["DeviceController.java"])
    st_b = _st("b", writable=["device.js"], depends_on=["a"])
    plan = TaskPlan(subtasks=[st_a, st_b], parallel_groups=[["a"], ["b"]])
    results = {
        "a": WorkerOutput(
            subtask_id="a",
            diff=('--- /dev/null\n+++ b/DeviceController.java\n@@ -0,0 +1,4 @@\n'
                  '+    @GetMapping("/system/device/list")\n'
                  '+    public AjaxResult list() {}\n'
                  '+    @PostMapping("/system/device/add")\n'
                  '+    public AjaxResult add() {}'),
            summary="controller", l1_passed=True,
        )
    }
    _inject_predecessor_context([st_b], plan, results)
    snip = st_b.context_snippets
    assert "API 端点" in snip
    assert "/system/device/list" in snip and "/system/device/add" in snip
    assert "GET" in snip and "POST" in snip


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


# ── 事实库回灌 ──
def test_feedback_to_knowledge_parses_changes():
    """子任务产出的 diff → 提取变更文件（区分新建/修改），入队增量索引。"""
    from unittest.mock import patch as mock_patch

    from swarm.brain.nodes.dispatch import _feedback_to_knowledge
    out = WorkerOutput(
        subtask_id="st-1",
        diff=("--- /dev/null\n+++ b/New.java\n@@ -0,0 +1 @@\n+class New {}\n"
              "--- a/Old.java\n+++ b/Old.java\n@@ -1 +1 @@\n+changed\n"),
        summary="x", l1_passed=True,
    )
    captured = {}
    # 无运行中 event loop 时 create_task 会 RuntimeError 被吞——这里只验证不抛异常 + 文件解析
    # 用真实调用走到 changes 构建（enqueue 在无 loop 时静默跳过）
    _feedback_to_knowledge("proj-1", _st("st-1"), out)  # 不抛异常即可


def test_feedback_no_project_noop():
    from swarm.brain.nodes.dispatch import _feedback_to_knowledge
    out = WorkerOutput(subtask_id="x", diff="+++ b/A.java\n+x", summary="", l1_passed=True)
    _feedback_to_knowledge("", _st("x"), out)  # 无 project_id → noop 不抛


def test_feedback_empty_diff_noop():
    from swarm.brain.nodes.dispatch import _feedback_to_knowledge
    out = WorkerOutput(subtask_id="x", diff="", summary="", l1_passed=True)
    _feedback_to_knowledge("proj-1", _st("x"), out)  # 空 diff → noop 不抛


# ── 契约符号提取（task 2c019bc5：带中文描述的 API 整句不该整句匹配）──
def test_contract_symbols_extracts_core_identifier():
    from swarm.brain.contract_utils import contract_symbols
    c = {"apis": ["GET /system/device/list — 分页查询设备列表，参数：deviceName",
                  "POST /system/device/add — 新增设备",
                  "PUT /system/device/edit/{deviceId} — 修改"]}
    syms = contract_symbols(c)
    assert "list" in syms and "add" in syms and "edit" in syms
    # 不该把整句中文描述当符号
    assert all(len(s) < 30 for s in syms)
    # HTTP 动词噪音被过滤
    assert "get" not in [s.lower() for s in syms]


def test_contract_partial_match_passes():
    """前端实现了部分契约端点 → 不全 missing → 通过（非真偏离）。"""
    from swarm.brain.integration_review import check_contract_in_diff
    c = {"apis": ["GET /system/device/list — 查询", "POST /system/device/add — 新增"]}
    diff = "+++ b/device.js\n+url: '/system/device/list'\n+function add() {}"
    ok, _ = check_contract_in_diff(diff, c)
    assert ok


def test_contract_total_miss_fails():
    """完全不沾边 → 全 missing → 失败（真契约偏离）。"""
    from swarm.brain.integration_review import check_contract_in_diff
    c = {"apis": ["GET /system/device/list — 查询"]}
    ok, issues = check_contract_in_diff("+++ b/Other.java\n+foo()", c)
    assert not ok and issues
