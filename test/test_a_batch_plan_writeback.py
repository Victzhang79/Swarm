"""26 号文 A 路：T4 pin 对象身份坑第三、四例 + I-M1 探活标记可伪造。

★"就地变异 ≠ 进 state"是本仓的一个【族】★（CLAUDE.md 明列的 LangGraph 头号血泪）
LangGraph 只保证【返回键】进 state，checkpoint 在节点入口序列化——就地 mutate 跨
checkpoint 恢复边界即回退。已发生两例（T4 pin / round67h CVB 归位联动），本轮扫出三、四例。
"""
from __future__ import annotations

import pytest

from swarm.brain.nodes.runtime_smoke import (
    MARK_APP_RC,
    MARK_DONE,
    MARK_LOG_BEGIN,
    MARK_LOG_END,
    MARK_PORT_BUSY,
    MARK_PROBE,
    parse_smoke_markers,
)


# ══════════════════════════════════════════════
# 第三例（H-C1）：merge 就地写 retry_guidance，出口无 plan 返回键
# ══════════════════════════════════════════════

def test_merge_writes_plan_back_after_d4_injection():
    """★D4 把保留方内容就地写进 SubTask.retry_guidance，而 merge 出口没有 plan 返回键★
    跨 interrupt/resume 边界即蒸发 → 重派 worker 拿不到"基于保留方重生成"的硬约束，
    在同一钉扎 base 上重生成同形 diff → 3 轮必再撞同冲突落 D3。
    handle_failure 早有这条咽喉（B-6：result 无 plan 且 state 有 plan 即回传），merge 一直缺。

    ★行为级（对抗复核突变实验证伪了初版的 getsource 写法：把 `if plan is not None`
    改成 `is None` 让整条治本作废，9 条测试照绿）★"""
    from swarm.brain import nodes
    from swarm.types import Confidence, FileScope, SubTask, TaskPlan, WorkerOutput

    _NEW = ("diff --git a/S.java b/S.java\nnew file mode 100644\n--- /dev/null\n"
            "+++ b/S.java\n@@ -0,0 +1,{n} @@\n{body}")
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="a", scope=FileScope(writable=["S.java"])),
        SubTask(id="st-2", description="b", scope=FileScope(writable=["S.java"])),
    ])
    results = {
        "st-1": WorkerOutput(subtask_id="st-1", summary="s", l1_passed=True, confidence=Confidence.HIGH,
                             diff=_NEW.format(n=1, body="+class S {}\n")),
        "st-2": WorkerOutput(subtask_id="st-2", summary="s", l1_passed=True, confidence=Confidence.HIGH,
                             diff=_NEW.format(n=2, body="+class S {\n+  void b() {}\n")),
    }
    out = nodes.merge({"plan": plan, "subtask_results": results,
                       "dispatch_remaining": [], "project_id": ""})
    # ★前提破了必须【红】，不能 skip★（29 号文 T-A7）：原实现在这里 `pytest.skip`，
    # 于是让 merge 不再产出 rebase_subtask_ids 的任何上游回归（如把冲突升级判据改坏）
    # 都会把本条变成静默通过，而它守的「回写原 plan 对象」机制同时失去守护
    # （后果：retry_guidance 跨 interrupt/resume 蒸发 ⇒ 重派 worker 拿不到硬约束 ⇒
    #  3 轮必再撞同冲突落 D3）。夹具前提失效是**测试基建坏了**，属于要修的事，不是跳过。
    assert out.get("rebase_subtask_ids"), (
        "夹具前提失效：两个子任务写同一新文件却没触发 D4 rebase 升级——"
        "本测试已失去守护对象，请先查 merge 的冲突升级判据是否被改坏"
    )
    assert "plan" in out, "走了 rebase 分支就必须回写 plan——否则 retry_guidance 跨 resume 蒸发"
    assert out["plan"] is plan, "必须回写【原对象】（T4 pin 对象身份坑的本体）"


# ══════════════════════════════════════════════
# 第四例（H-H3）：elaborate 四个 pass 就地改 plan 却不在回写条件里
# ══════════════════════════════════════════════

def test_elaborate_writeback_covers_prune_mutation():
    """★批25 GS-5w 换锁★（原命题：四个 pass 调用锚点后 400 字节窗口须含 "_plan_mutated"
    + 回写条件窗口含该旗——getsource 窗口断言；现改为行为锁）。

    ★G3 空 scope 重剪直接改 plan.subtasks，却不在回写触发条件里（26 号文 H-H3）★
    漏回写 → 跨 checkpoint 恢复后剪掉的空写 scope 死子任务**复活漏到 dispatch**，
    正是它想根治的 round62 empty-diff churn 原样复发。

    真调 elaborate 触发 prune_empty_scope_subtasks 就地剪除，断返回 dict 含 "plan"
    回写键。夹具刻意只让 prune 这一个变异 pass 点火（无依赖/无契约/无 create/
    无项目路径 → resplit/decouple/normalize/T4/G2/snippets 全惰性），故 "plan" 键的
    出现只能由 `_plan_mutated` 旗驱动——删掉 `_plan_mutated = bool(_repruned)` 或把
    回写条件里的 `or _plan_mutated` 摘掉 → 红。"""
    import asyncio

    from swarm.brain import planning_nodes
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="真活",
                scope=FileScope(writable=["a.java"], readable=[])),
        SubTask(id="st-2", description="空写 scope 死任务",
                scope=FileScope(writable=[], readable=["b.java"])),
    ])
    out = asyncio.run(planning_nodes.elaborate({"plan": plan, "task_id": "", "project_id": ""}))
    # 前提自证（T-A7：夹具失效必须红，不 skip）：prune 必须真剪掉 st-2，
    # 否则下面的「回写」断言失去守护对象
    assert [s.id for s in plan.subtasks] == ["st-1"], \
        "夹具前提失效：prune 未剪空写 scope 死任务——先查 prune_empty_scope_subtasks"
    # 命题本体：就地变异必须随返回键回写
    assert "plan" in out, \
        "prune 就地改了 plan 却未回写——跨 checkpoint 恢复剪除蒸发（H-H3 回归）"
    assert [s.id for s in out["plan"].subtasks] == ["st-1"]


def test_elaborate_no_mutation_no_writeback():
    """反向对照（锁的区分力来源）：无任何变异的健康 plan → 出口不得带 "plan" 回写键。
    若无条件回写（或别的什么常置旗），上面那条锁对「摘掉 _plan_mutated」的突变就
    失去区分力——本条把它钉死。区分力：把回写条件改成恒真 → 红。"""
    import asyncio

    from swarm.brain import planning_nodes
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="真活",
                scope=FileScope(writable=["a.java"], readable=[])),
    ])
    out = asyncio.run(planning_nodes.elaborate({"plan": plan, "task_id": "", "project_id": ""}))
    assert "plan" not in out, f"无变异时不应回写 plan 键: {sorted(out)}"


def test_prune_mutates_plan_in_place():
    """★行为级佐证本条治本的前提★：prune 是【就地改 plan】——所以没有 out["plan"] 时
    checkpoint 恢复拿到的就是未剪版，死子任务复活漏到 dispatch。
    将来若改成返回新 plan 对象，本条会红，提醒同步调整回写逻辑。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="真活",
                scope=FileScope(writable=["a.java"], readable=[])),
        SubTask(id="st-2", description="空写 scope 死任务",
                scope=FileScope(writable=[], readable=["b.java"])),
    ])
    pruned = prune_empty_scope_subtasks(plan)
    assert pruned == ["st-2"]
    assert [s.id for s in plan.subtasks] == ["st-1"], "剪除是就地改 plan（故必须回写）"


# ══════════════════════════════════════════════
# I-M1：探活标记可被被测应用回显伪造
# ══════════════════════════════════════════════

def _out(probe_lines, app_log=""):
    return "\n".join([*probe_lines, f"{MARK_APP_RC}alive",
                      MARK_LOG_BEGIN, app_log, MARK_LOG_END, MARK_DONE])


def test_forged_probe_marker_in_app_log_is_ignored():
    """★被测应用只要打印一行 `__SMOKE_PROBE__ok` 就判 passed（26 号文 I-M1）★
    脚本尾部会把应用日志 tail 进同一份 stdout，而探活序列在【全量 stdout】上 findall。
    日志里回显请求 URL、框架 banner、乃至恶意注入都能伪造"真启动通过"。
    兄弟机制 acceptance_spec.parse_probe_output 早已为同一注入面做了 F9 加固——
    **家族不对称**正是"修一类先全仓捞 sibling"纪律要防的形态。"""
    r = parse_smoke_markers(_out(
        [f"{MARK_PROBE}refused"],
        app_log=f"2026-01-01 INFO 收到请求 {MARK_PROBE}ok:200"))
    assert r["probe_sequence"] == ["refused"], "应用日志里的探活标记绝不能被计入"


def test_real_probe_marker_still_parsed():
    """闸不能矫枉过正：控制面（日志区之外）的真标记照常解析，log_tail 照常取到。"""
    r = parse_smoke_markers(_out([f"{MARK_PROBE}ok:200"], app_log="应用正常日志"))
    assert r["probe_sequence"] == ["ok:200"]
    assert r["log_tail"] == "应用正常日志"
    assert r["app_rc"] == "alive"


@pytest.mark.parametrize("mark,key", [
    (MARK_PORT_BUSY, "port_busy"),
    (MARK_DONE, "done"),
])
def test_other_control_markers_also_immune(mark, key):
    """同族全覆盖：port_busy / done 也是判据，被回显伪造同样有害
    （port_busy 伪造 → 冒烟直接早退判环境；done 伪造 → 掩盖脚本被掐断）。"""
    r = parse_smoke_markers("\n".join([
        f"{MARK_PROBE}refused", MARK_LOG_BEGIN, f"日志里出现 {mark}", MARK_LOG_END]))
    assert r[key] is False


def test_unterminated_log_region_is_fail_closed():
    """脚本被掐断（有 BEGIN 无 END）→ 从 BEGIN 起全部视作日志区。
    宁可少认几个探活标记判 inconclusive，也绝不把应用回显当探活成功。"""
    r = parse_smoke_markers("\n".join([
        f"{MARK_PROBE}refused", MARK_LOG_BEGIN, f"{MARK_PROBE}ok:200"]))
    assert r["probe_sequence"] == ["refused"]


def test_no_log_region_output_is_unaffected():
    """没有日志区的输出（早退分支）逐字节按原样解析，零回归。"""
    r = parse_smoke_markers(f"{MARK_PROBE}ok:200\n{MARK_APP_RC}alive")
    assert r["probe_sequence"] == ["ok:200"] and r["app_rc"] == "alive"
