"""task 34fab09e 方案A+B1 回归：
- A: enrich_context_snippets 把 scope 文件真实代码抽进 subtask.context_snippets
- B1: 非 trivial 任务按 scope 文件数动态加 max_iterations
"""
import os
import tempfile

from swarm.brain.contract_utils import enrich_context_snippets
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


def _mk_proj():
    d = tempfile.mkdtemp()
    # 一个"参照文件"(小,应给全文) + 一个"待改文件"(给签名)
    util_dir = os.path.join(d, "util")
    os.makedirs(util_dir)
    with open(os.path.join(util_dir, "ExcelUtil.java"), "w") as f:
        f.write("public class ExcelUtil {\n    public void exportExcel(List data) {}\n}\n")
    ctrl_dir = os.path.join(d, "ctrl")
    os.makedirs(ctrl_dir)
    with open(os.path.join(ctrl_dir, "LogController.java"), "w") as f:
        f.write("public class LogController extends BaseController {\n"
                "    public AjaxResult list() { return success(); }\n}\n")
    return d


def test_enrich_context_snippets_injects_code():
    d = _mk_proj()
    st = SubTask(
        id="st-1", description="导出日志",
        scope=FileScope(writable=["ctrl/LogController.java"], readable=["util/ExcelUtil.java"]),
    )
    plan = TaskPlan(subtasks=[st])
    changed = enrich_context_snippets(plan, d)
    assert changed
    snip = plan.subtasks[0].context_snippets
    # 参照文件 ExcelUtil 应给全文（含方法体），待改文件 LogController 应给签名
    assert "ExcelUtil" in snip
    assert "exportExcel" in snip
    assert "LogController" in snip
    assert "参照文件" in snip
    assert "待修改文件" in snip


def test_enrich_no_project_path_noop():
    st = SubTask(id="st-1", description="x", scope=FileScope(writable=["a.java"]))
    plan = TaskPlan(subtasks=[st])
    assert enrich_context_snippets(plan, None) is False
    assert plan.subtasks[0].context_snippets == ""


def test_enrich_idempotent():
    """已有 context_snippets 不覆盖（replan 幂等）。"""
    d = _mk_proj()
    st = SubTask(id="st-1", description="x",
                 scope=FileScope(readable=["util/ExcelUtil.java"]),
                 context_snippets="已存在的内容")
    plan = TaskPlan(subtasks=[st])
    enrich_context_snippets(plan, d)
    assert plan.subtasks[0].context_snippets == "已存在的内容"


def test_b1_dynamic_max_iterations():
    """B1: 非 trivial 多文件任务 max_iterations 按文件数动态增加，封顶 100。"""
    from swarm.worker.executor import WorkerExecutor
    # 4 文件 medium 任务：base(配置默认,通常50) + 4*15，但封顶 100
    st = SubTask(
        id="st-1", description="导出功能", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["a.java", "b.java", "c.java", "d.java"]),
    )
    w = WorkerExecutor(st, task_id="t1")
    # 至少应比 base 高（4 文件 +60，但封顶 100）
    assert w.max_iterations > 50 or w.max_iterations == 100, f"got {w.max_iterations}"
    assert w.max_iterations <= 100

    # 单文件不加
    st1 = SubTask(id="st-2", description="改一个", difficulty=SubTaskDifficulty.MEDIUM,
                  scope=FileScope(writable=["only.java"]))
    w1 = WorkerExecutor(st1, task_id="t2")
    # 单文件不触发动态加成（_nfiles>1 才加）
    from swarm.config import get_config
    assert w1.max_iterations == get_config().worker.max_iterations

    # trivial 仍封顶 30
    st2 = SubTask(id="st-3", description="trivial", difficulty=SubTaskDifficulty.TRIVIAL,
                  scope=FileScope(writable=["a.java", "b.java", "c.java", "d.java"]))
    w2 = WorkerExecutor(st2, task_id="t3")
    assert w2.max_iterations <= 30


def test_finding12_recursion_boost_lifts_trivial_cap():
    """FINDING-12：force_strong 子任务重试时 recursion_boost 抬高 trivial 步数上限。

    RUN5 死在 trivial 档 recursion_limit(~30) `Sorry, need more steps`。Feature B 只换最强
    模型不够——还须给更多步数。boost=30 把 trivial 30→60；boost=0 保持零行为差。
    """
    from swarm.worker.executor import WorkerExecutor

    st = SubTask(id="st-1", description="trivial", difficulty=SubTaskDifficulty.TRIVIAL,
                 scope=FileScope(writable=["a.java"]))

    # 不 boost：trivial 仍封顶 30（默认零行为差）
    w0 = WorkerExecutor(st, task_id="t0", recursion_boost=0)
    assert w0.max_iterations <= 30, f"无 boost 应≤30: {w0.max_iterations}"

    # boost=30：trivial 30 → 60（拒答重试给够步数，最强模型才有机会在步内收敛）
    w1 = WorkerExecutor(st, task_id="t1", recursion_boost=30)
    assert w1.max_iterations == w0.max_iterations + 30, \
        f"boost 应在原上限上加 30: {w0.max_iterations} → {w1.max_iterations}"
    assert w1.max_iterations > 30, "boost 后必须突破 trivial 30 档，否则照样撞墙"

    # 封顶 150：超大 boost 不会让步数无限膨胀
    w2 = WorkerExecutor(st, task_id="t2", recursion_boost=1000)
    assert w2.max_iterations == 150, f"boost 封顶 150: {w2.max_iterations}"


def test_enrich_create_subtask_gets_existing_layer_reference():
    """治本 RUN11：纯 CREATE 子任务(writable/readable 皆空)→ 旧 enrich 产出空 context_snippets
    → worker 探索全项目找模式烧光 600s 预算。新逻辑：给 create 文件找【同类既有文件】作范例预读，
    照 RuoYi 写法实现，无需探索。"""
    with tempfile.TemporaryDirectory() as d:
        # 既有参照：system 模块的一个 entity（与待建 entity 同层）
        ref_dir = os.path.join(d, "mod-system", "src", "main", "java", "com", "x", "system", "domain")
        os.makedirs(ref_dir)
        with open(os.path.join(ref_dir, "SysFoo.java"), "w") as f:
            f.write("package com.x.system.domain;\npublic class SysFoo extends BaseEntity {\n  private Long id;\n  public Long getId(){return id;}\n}\n")
        # 待建：alarm 模块的 entity（纯 create，无 writable/readable）
        st = SubTask(
            id="st-1", description="创建 AlarmFoo 实体",
            scope=FileScope(create_files=["mod-alarm/src/main/java/com/x/alarm/domain/AlarmFoo.java"]),
        )
        plan = TaskPlan(subtasks=[st])
        changed = enrich_context_snippets(plan, d)
        assert changed is True, "应为 create 子任务注入范例"
        cs = st.context_snippets or ""
        assert "SysFoo" in cs and "同类既有范例" in cs, f"应预读同层既有 entity 作模板: {cs[:200]}"
