"""26 号文 A 路：T4 pin 对象身份坑第三、四例 + I-M1 探活标记可伪造。

★"就地变异 ≠ 进 state"是本仓的一个【族】★（CLAUDE.md 明列的 LangGraph 头号血泪）
LangGraph 只保证【返回键】进 state，checkpoint 在节点入口序列化——就地 mutate 跨
checkpoint 恢复边界即回退。已发生两例（T4 pin / round67h CVB 归位联动），本轮扫出三、四例。
"""
from __future__ import annotations

import inspect

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
    handle_failure 早有这条咽喉（B-6：result 无 plan 且 state 有 plan 即回传），merge 一直缺。"""
    from swarm.brain import nodes
    src = inspect.getsource(nodes.merge)
    i_d4 = src.index("_st.retry_guidance = (")
    i_wb = src.index('out["plan"] = plan')
    assert i_d4 < i_wb, "回写必须在 D4 注入之后"
    # 且在 rebase 分支内（clean 路径不该无谓回写）
    assert 'out["dispatch_remaining"] = dispatch_remaining' in src[:i_wb]


# ══════════════════════════════════════════════
# 第四例（H-H3）：elaborate 四个 pass 就地改 plan 却不在回写条件里
# ══════════════════════════════════════════════

def test_elaborate_writeback_covers_every_mutating_pass():
    """★G3 空 scope 重剪直接改 plan.subtasks，却不在回写触发条件里（26 号文 H-H3）★
    漏回写 → 跨 checkpoint 恢复后剪掉的空写 scope 死子任务**复活漏到 dispatch**，
    正是它想根治的 round62 empty-diff churn 原样复发。
    另三个同样漏网：意图校正、context_snippets 注入、API 知识注入。"""
    from swarm.brain import planning_nodes
    src = inspect.getsource(planning_nodes.elaborate)
    assert "_plan_mutated" in src
    # 四个 pass 都必须把结果并进回写旗
    for anchor in ("prune_empty_scope_subtasks(plan_obj)",
                   "correct_misclassified_intent(plan_obj)",
                   "enrich_context_snippets(plan_obj",
                   "inject_api_knowledge(plan_obj)"):
        i = src.index(anchor)
        window = src[i:i + 400]
        assert "_plan_mutated" in window, f"{anchor} 的结果未并入回写旗"
    # 回写条件本身必须含该旗
    i_cond = src.index("or _prov_added")
    assert "_plan_mutated" in src[i_cond:i_cond + 60]


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
