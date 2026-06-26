"""Brain Graph — 构建 LangGraph StateGraph，注册节点和边，编译为可执行 graph

状态机流转:
  RECEIVED → ANALYZE → PLAN → VALIDATE_PLAN →
    [CONFIRM (ultra)] → DISPATCH → MONITOR →
      [HAS_FAILURE → HANDLE_FAILURE → DISPATCH |
       HAS_MORE → DISPATCH |
       ALL_DONE → MERGE → VERIFY_L2 → [L2 gate] VERIFY_L3 → DELIVER →
         [ACCEPT → LEARN_SUCCESS |
          REVISE  → REVISION → DISPATCH |
          REJECT  → LEARN_FAILURE] → DONE]

条件边:
  - after_validate: plan_valid? → CONFIRM(ultra)/DISPATCH(non-ultra) / PLAN(retry)
  - after_confirm: human_decision? → DISPATCH(accept) / DONE(reject)
  - after_monitor: has_failures? → HANDLE_FAILURE / has_more? → DISPATCH / MERGE
  - after_merge: merge_conflicts? → HANDLE_FAILURE / VERIFY_L2
  - after_verify_l2: l2_passed? → VERIFY_L3 / HANDLE_FAILURE
  - after_verify_l3: l3_passed? → DELIVER / HANDLE_FAILURE
  - after_handle_failure: strategy? → DISPATCH(retry) / PLAN(replan) / DELIVER(escalate)
  - after_deliver: human_decision? → LEARN_SUCCESS/REVISION/LEARN_FAILURE
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph

from swarm.brain.nodes import (
    analyze,
    confirm_plan,
    deliver,
    dispatch,
    handle_failure,
    learn_failure,
    learn_success,
    merge,
    monitor,
    plan,
    revision,
    validate_plan,
    verify_l2,
    verify_l3,
)
from swarm.brain.planning_nodes import (
    assess,
    clarify,
    contract_design,
    detect_stack,
    elaborate,
    review_design,
    tech_design,
)
from swarm.brain.ingest_node import ingest
from swarm.brain.state import BrainState, effective_complexity
from swarm.config.settings import get_config
from swarm.types import Complexity, HumanDecision

logger = logging.getLogger(__name__)

# 最大计划重试次数
MAX_PLAN_RETRY = 3

# 进程内单例 — 共享 checkpointer，支持跨 HTTP 请求 interrupt/resume
_compiled_brain_graph = None
_memory_checkpointer = None
# A1 批1：PG checkpointer 单例 + 其 context manager（连接生命周期 = app 生命周期）。
# 多副本共享同一 PG checkpoint 表，实现跨副本 interrupt/resume。
_pg_checkpointer = None
_pg_checkpointer_cm = None  # AsyncPostgresSaver.from_conn_string(...) 的 cm，shutdown 时 __aexit__


# ══════════════════════════════════════════════
# 条件边函数
# ══════════════════════════════════════════════


def after_analyze(state: BrainState) -> Literal["clarify", "tech_design"]:
    """ANALYZE 后路由（Q4 规划子图入口）：
    - needs_clarify(非微任务&非自动化) → CLARIFY（进交互式渐进规划，澄清后经 assess→tech_design）
    - 其余（含微任务/自动化/直达）→ TECH_DESIGN（需求转化层：事实核验 + 文件级方案）

    需求转化/技术设计前置阶段：所有任务进 PLAN 前都先过 tech_design。它做两件事——
    ① 事实核验（虚假前提检测，如"改不存在的文件"）；② 产品需求→文件级技术方案。
    任何级别的任务都可能有虚假前提，故不分难度都核验（轻量核验廉价，转化按需）。
    """
    if state.get("needs_clarify") and not state.get("is_micro_task"):
        logger.info("[ROUTE] ANALYZE → CLARIFY (进规划子图)")
        return "clarify"
    logger.info("[ROUTE] ANALYZE → TECH_DESIGN (需求转化/事实核验前置)")
    return "tech_design"


def after_clarify(state: BrainState) -> Literal["clarify", "assess", "deliver"]:
    """CLARIFY 后路由（多轮循环）：
    - clarify_blocked_by_facts → DELIVER（虚假前提阻断：不进规划，直接报告"基于虚假事实无法执行"）
    - clarify_done → ASSESS（澄清完成，定级）
    - 否则 → CLARIFY（继续下一轮，LLM 再评估是否还要问）
    """
    if state.get("clarify_blocked_by_facts"):
        logger.warning("[ROUTE] CLARIFY → DELIVER (虚假前提阻断，终止并报告)")
        return "deliver"
    if state.get("clarify_done"):
        logger.info("[ROUTE] CLARIFY → ASSESS (澄清完成)")
        return "assess"
    logger.info("[ROUTE] CLARIFY → CLARIFY (下一轮澄清)")
    return "clarify"


def after_assess(state: BrainState) -> Literal["tech_design", "plan"]:
    """ASSESS 后路由（澄清后定级）：
    - complex/ultra → TECH_DESIGN（出技术方案+评审）
    - simple/medium → PLAN（轻量直达）
    """
    comp = state.get("assessed_complexity") or state.get("complexity", Complexity.MEDIUM)
    if comp in (Complexity.COMPLEX, Complexity.ULTRA):
        logger.info("[ROUTE] ASSESS → TECH_DESIGN (%s)", comp.value)
        return "tech_design"
    logger.info("[ROUTE] ASSESS → PLAN (%s 轻量)", comp.value)
    return "plan"


def after_review(state: BrainState) -> Literal["tech_design", "plan"]:
    """REVIEW_DESIGN 后路由：
    - 评审通过(approve) → PLAN
    - 打回(reject) → TECH_DESIGN（带反馈重做；打回上限由节点内强制 approve 收敛）
    """
    review = state.get("design_review") or {}
    if review.get("decision") == "reject":
        logger.info("[ROUTE] REVIEW → TECH_DESIGN (打回重做)")
        return "tech_design"
    logger.info("[ROUTE] REVIEW → PLAN (方案通过)")
    return "plan"


def after_tech_design(state: BrainState) -> Literal["clarify", "review_design"]:
    """TECH_DESIGN 后路由（需求转化层出口）：
    - 检出【虚假前提】（fact_issues 含 verdict=false）→ CLARIFY 强制澄清。
      事实层不确定必须打断自动化——基于虚假前提自动跑下去只会产垃圾、烧算力。
      这会【覆盖 auto_accept】：事实存疑就不能自动接受（用户决策："涉及事实需澄清，就不能 auto_accept"）。
    - 无虚假前提 → REVIEW_DESIGN（原流程：人工评审/auto_accept 自动通过 → plan）。
    """
    fact_issues = state.get("tech_design_fact_issues") or []
    # 治本：block 必须【确定性坐实】——只阻断 grounded=True（磁盘核验坐实点名文件缺失）的虚假前提。
    # 纯 LLM 自由文本判定（框架/栈差异、语义臆测）grounded=False → advisory，不阻断 auto_accept
    # （框架/栈维度由 detect_stack 权威拥有，"不以文档为准"）。tech_design 已标好 grounded。
    false_premises = [
        fi for fi in fact_issues
        if isinstance(fi, dict) and fi.get("verdict") == "false" and fi.get("grounded")
    ]
    if false_premises:
        logger.warning(
            "[ROUTE] TECH_DESIGN → CLARIFY (检出 %d 个【确定性坐实】虚假前提，强制澄清，覆盖 auto_accept): %s",
            len(false_premises),
            [fi.get("claim", "?") for fi in false_premises][:3],
        )
        return "clarify"
    _advisory = [fi for fi in fact_issues
                 if isinstance(fi, dict) and fi.get("verdict") == "false" and not fi.get("grounded")]
    logger.info(
        "[ROUTE] TECH_DESIGN → REVIEW_DESIGN (无确定性坐实虚假前提%s)",
        f"；%d 个未坐实 verdict=false 降级 advisory 不阻断" % len(_advisory) if _advisory else "",
    )
    return "review_design"


def after_validate(state: BrainState) -> Literal["confirm", "plan", "dispatch"]:
    """VALIDATE_PLAN 后的路由:

    - plan_valid=False & 重试次数未达上限 → 重新 PLAN
    - plan_valid=True & complexity=ultra → CONFIRM (人工确认)
    - plan_valid=True & complexity!=ultra → 直接 DISPATCH
    """
    plan_valid = state.get("plan_valid", False)
    retry_count = state.get("plan_retry_count", 0)
    complexity = effective_complexity(state)  # 修复 12.3：澄清后定级优先，避免漏 ultra 确认闸门

    if not plan_valid and retry_count < MAX_PLAN_RETRY:
        logger.info(f"[ROUTE] VALIDATE → PLAN (重试 {retry_count + 1}/{MAX_PLAN_RETRY})")
        return "plan"

    if not plan_valid and retry_count >= MAX_PLAN_RETRY:
        logger.warning("[ROUTE] VALIDATE → CONFIRM (计划校验失败，需人工确认)")
        return "confirm"

    if complexity == Complexity.ULTRA:
        logger.info("[ROUTE] VALIDATE → CONFIRM (ultra 复杂度)")
        return "confirm"

    logger.info("[ROUTE] VALIDATE → DISPATCH")
    return "dispatch"


def after_confirm(state: BrainState) -> Literal["dispatch", "end", "plan"]:
    """CONFIRM 后的路由:

    - human_decision=ACCEPT → DISPATCH
    - human_decision=REJECT → END
    - human_decision=REVISE → PLAN (重新规划)
    """
    decision = state.get("human_decision")

    if decision == HumanDecision.ACCEPT:
        logger.info("[ROUTE] CONFIRM → DISPATCH (accepted)")
        return "dispatch"
    elif decision == HumanDecision.REJECT:
        logger.info("[ROUTE] CONFIRM → END (rejected)")
        return "end"
    else:
        logger.info("[ROUTE] CONFIRM → PLAN (revise/re-plan)")
        return "plan"


def after_monitor(state: BrainState) -> Literal["handle_failure", "dispatch", "merge"]:
    """MONITOR 后的路由:

    - 有失败子任务 → HANDLE_FAILURE
    - 还有未派发子任务 → DISPATCH (继续下一批)
    - 全部完成 → MERGE
    """
    dispatch_remaining = state.get("dispatch_remaining", [])
    failed_ids = state.get("failed_subtask_ids", [])

    if failed_ids:
        logger.info(f"[ROUTE] MONITOR → HANDLE_FAILURE ({len(failed_ids)} 个失败)")
        return "handle_failure"

    if dispatch_remaining:
        logger.info(f"[ROUTE] MONITOR → DISPATCH ({len(dispatch_remaining)} 个剩余)")
        return "dispatch"

    logger.info("[ROUTE] MONITOR → MERGE (全部完成)")
    return "merge"


def after_merge(state: BrainState) -> Literal["handle_failure", "verify_l2", "dispatch", "deliver"]:
    """MERGE 后的路由:

    - failure_escalated + escalate → DELIVER（rebase 超限已升级人工，escalate 终点）
    - merge_conflicts 非空 → HANDLE_FAILURE（failed_subtask_ids 已由 merge 节点填充）
    - rebase_subtask_ids 非空（无硬冲突）→ DISPATCH（rebase 子任务已加入 dispatch_remaining，需重跑）
    - 无冲突无 rebase → VERIFY_L2
    """
    # TD2606-A6：rebase 超限时 merge 节点已设 failure_escalated/failure_strategy=escalate 但
    # 不会设 merge_conflicts/rebase_subtask_ids → 旧逻辑落到 VERIFY_L2，escalate 信号被丢 →
    # MERGE↔VERIFY_L2↔HANDLE_FAILURE 死循环烧 recursion_limit。直接路由 DELIVER（与
    # after_handle_failure 的 escalate→deliver 同构，可被 can_auto_accept_delivery 如实归因）。
    if state.get("failure_escalated") and state.get("failure_strategy") == "escalate":
        logger.warning("[ROUTE] MERGE → DELIVER (rebase 超限升级人工 escalate，避免死循环 A6)")
        return "deliver"

    conflicts = state.get("merge_conflicts", [])
    if conflicts:
        logger.info(
            "[ROUTE] MERGE → HANDLE_FAILURE (%d 个冲突)",
            len(conflicts),
        )
        return "handle_failure"

    rebase_ids = state.get("rebase_subtask_ids", [])
    if rebase_ids:
        logger.info(
            "[ROUTE] MERGE → DISPATCH (%d 个 rebase 子任务需重生成)",
            len(rebase_ids),
        )
        return "dispatch"

    logger.info("[ROUTE] MERGE → VERIFY_L2")
    return "verify_l2"


def after_verify_l2(state: BrainState) -> Literal["verify_l3", "handle_failure"]:
    """VERIFY_L2 后的路由（P0 L2 gate）:

    - l2_passed=True → VERIFY_L3
    - l2_passed=False → HANDLE_FAILURE（禁止进入 deliver）
    """
    if state.get("l2_passed", False):
        logger.info("[ROUTE] VERIFY_L2 → VERIFY_L3 (L2 通过)")
        return "verify_l3"

    logger.warning("[ROUTE] VERIFY_L2 → HANDLE_FAILURE (L2 未通过，阻断交付)")
    return "handle_failure"


def after_verify_l3(state: BrainState) -> Literal["deliver", "handle_failure"]:
    """VERIFY_L3 后的路由（P1 L3 gate）:

    - l3_skipped 或 l3_passed=True/None → DELIVER
    - l3_passed=False → HANDLE_FAILURE
    """
    if state.get("l3_skipped"):
        logger.info("[ROUTE] VERIFY_L3 → DELIVER (L3 跳过)")
        return "deliver"
    if state.get("l3_passed") is False:
        logger.warning("[ROUTE] VERIFY_L3 → HANDLE_FAILURE (L3 未通过)")
        return "handle_failure"
    logger.info("[ROUTE] VERIFY_L3 → DELIVER")
    return "deliver"


def after_handle_failure(state: BrainState) -> Literal["dispatch", "plan", "deliver"]:
    """HANDLE_FAILURE 后的路由:

    - failure_strategy=replan → PLAN
    - failure_strategy=escalate → DELIVER（l2_passed=False，人工审核）
    - retry / retry_alternate → DISPATCH
    """
    strategy = state.get("failure_strategy", "retry")

    if strategy == "replan":
        logger.info("[ROUTE] HANDLE_FAILURE → PLAN (replan)")
        return "plan"
    if strategy == "escalate":
        logger.info("[ROUTE] HANDLE_FAILURE → DELIVER (escalate)")
        return "deliver"

    logger.info("[ROUTE] HANDLE_FAILURE → DISPATCH (%s)", strategy)
    return "dispatch"


def after_deliver(state: BrainState) -> Literal["learn_success", "revision", "learn_failure"]:
    """DELIVER 后的路由:

    - ACCEPT → LEARN_SUCCESS
    - REVISE → REVISION
    - REJECT → LEARN_FAILURE
    """
    decision = state.get("human_decision")

    if decision == HumanDecision.ACCEPT:
        logger.info("[ROUTE] DELIVER → LEARN_SUCCESS")
        return "learn_success"
    elif decision == HumanDecision.REVISE:
        logger.info("[ROUTE] DELIVER → REVISION")
        return "revision"
    else:
        logger.info("[ROUTE] DELIVER → LEARN_FAILURE")
        return "learn_failure"


# ══════════════════════════════════════════════
# 状态增强节点
# ══════════════════════════════════════════════


def _increment_plan_retry(state: BrainState) -> dict:
    """计划重试计数器递增（用于 VALIDATE_PLAN → PLAN 的循环）"""
    return {"plan_retry_count": state.get("plan_retry_count", 0) + 1}


# ══════════════════════════════════════════════
# Graph 构建
# ══════════════════════════════════════════════


def build_brain_graph() -> StateGraph:
    """构建 Brain 状态机图

    Returns:
        编译好的 StateGraph，可调用 graph.invoke() / graph.ainvoke()
    """
    graph = StateGraph(BrainState)

    # ── 注册节点 ──
    graph.add_node("ingest", ingest)        # B 部分：多模态需求摄取（前置于 analyze）
    graph.add_node("analyze", analyze)
    graph.add_node("detect_stack", detect_stack)  # 2.7：技术栈/架构识别（plan 前预处理，磁盘 ground truth）
    # Q4 规划子图节点
    graph.add_node("clarify", clarify)
    graph.add_node("assess", assess)
    graph.add_node("tech_design", tech_design)
    graph.add_node("contract_design", contract_design)  # T1：多模块共享契约（Brain 大模型）
    graph.add_node("review_design", review_design)
    graph.add_node("elaborate", elaborate)
    graph.add_node("plan", plan)
    graph.add_node("validate_plan", validate_plan)
    graph.add_node("confirm", confirm_plan)
    graph.add_node("dispatch", dispatch)
    graph.add_node("monitor", monitor)
    graph.add_node("handle_failure", handle_failure)
    graph.add_node("merge", merge)
    graph.add_node("verify_l2", verify_l2)
    graph.add_node("verify_l3", verify_l3)
    graph.add_node("deliver", deliver)
    graph.add_node("revision", revision)
    graph.add_node("learn_success", learn_success)
    graph.add_node("learn_failure", learn_failure)
    graph.add_node("increment_retry", _increment_plan_retry)

    # ── 设置入口 ──
    graph.set_entry_point("ingest")          # B 部分：先摄取上传文件（无文件则直通）
    graph.add_edge("ingest", "analyze")      # 摄取后进入分析

    # ── 线性边 ──
    # analyze → plan 改为条件边（见下方 after_analyze，接入 Q4 规划子图）
    graph.add_edge("plan", "elaborate")          # plan 后做上下文预算/INVEST 后处理
    graph.add_edge("elaborate", "validate_plan")
    # 注意：confirm 的出口【完全】由 after_confirm 条件边决定（见下方 add_conditional_edges）。
    # 此处【绝不能】再加 graph.add_edge("confirm", "dispatch") —— LangGraph 会把同节点的
    # 静态边与条件边当 fan-out 并行触发：即使 after_confirm 返回 "end"(REJECT)，静态边仍会
    # 把流程拽进 dispatch，导致"计划已判非法 / 已 reject，执行层却照样派发"(task 37460a5b
    # 13 分钟空烧的根因)。该缺陷自初始提交起潜伏，直到 plan_invalid→CONFIRM(REJECT) 路径
    # 被启用才显形。test/test_confirm_fanout_topology.py 用图拓扑断言守护此不变量。
    graph.add_edge("revision", "dispatch")
    graph.add_edge("learn_success", END)
    graph.add_edge("learn_failure", END)

    # ── Q4 规划子图边 ──
    # ANALYZE → DETECT_STACK（先把"项目是什么栈"做成单一权威事实）→[条件] CLARIFY / TECH_DESIGN
    graph.add_edge("analyze", "detect_stack")
    graph.add_conditional_edges(
        "detect_stack",
        after_analyze,
        {"clarify": "clarify", "tech_design": "tech_design"},
    )
    # CLARIFY ⟲[条件] CLARIFY(多轮) / ASSESS
    graph.add_conditional_edges(
        "clarify",
        after_clarify,
        {"clarify": "clarify", "assess": "assess", "deliver": "deliver"},
    )
    # ASSESS →[条件] TECH_DESIGN / PLAN
    graph.add_conditional_edges(
        "assess",
        after_assess,
        {"tech_design": "tech_design", "plan": "contract_design"},
    )
    # TECH_DESIGN →[条件] CLARIFY(虚假前提，强制澄清) / REVIEW_DESIGN(核验通过)
    graph.add_conditional_edges(
        "tech_design",
        after_tech_design,
        {"clarify": "clarify", "review_design": "review_design"},
    )
    # REVIEW_DESIGN →[条件] TECH_DESIGN(打回) / CONTRACT_DESIGN→PLAN(通过)
    graph.add_conditional_edges(
        "review_design",
        after_review,
        {"tech_design": "tech_design", "plan": "contract_design"},
    )
    # T1：CONTRACT_DESIGN → PLAN（多模块共享契约设计后进入拆解；非多模块直通空契约）
    graph.add_edge("contract_design", "plan")

    # ── 条件边 ──

    # VALIDATE_PLAN → CONFIRM / PLAN(retry) / DISPATCH
    graph.add_conditional_edges(
        "validate_plan",
        after_validate,
        {
            "confirm": "confirm",
            "plan": "increment_retry",  # 重试前先递增计数器
            "dispatch": "dispatch",
        },
    )

    # increment_retry → plan (重试循环)
    graph.add_edge("increment_retry", "plan")

    # CONFIRM → DISPATCH / PLAN / END
    graph.add_conditional_edges(
        "confirm",
        after_confirm,
        {
            "dispatch": "dispatch",
            "end": END,
            "plan": "plan",
        },
    )

    # MONITOR → HANDLE_FAILURE / DISPATCH / MERGE
    graph.add_conditional_edges(
        "monitor",
        after_monitor,
        {
            "handle_failure": "handle_failure",
            "dispatch": "dispatch",
            "merge": "merge",
        },
    )

    # MERGE → HANDLE_FAILURE / DISPATCH(rebase) / VERIFY_L2
    graph.add_conditional_edges(
        "merge",
        after_merge,
        {
            "handle_failure": "handle_failure",
            "dispatch": "dispatch",
            "verify_l2": "verify_l2",
            "deliver": "deliver",
        },
    )

    # VERIFY_L2 → VERIFY_L3 / HANDLE_FAILURE (L2 gate)
    graph.add_conditional_edges(
        "verify_l2",
        after_verify_l2,
        {
            "verify_l3": "verify_l3",
            "handle_failure": "handle_failure",
        },
    )

    # VERIFY_L3 → DELIVER / HANDLE_FAILURE (L3 gate)
    graph.add_conditional_edges(
        "verify_l3",
        after_verify_l3,
        {
            "deliver": "deliver",
            "handle_failure": "handle_failure",
        },
    )

    # HANDLE_FAILURE → DISPATCH / PLAN / DELIVER
    graph.add_conditional_edges(
        "handle_failure",
        after_handle_failure,
        {
            "dispatch": "dispatch",
            "plan": "plan",
            "deliver": "deliver",
        },
    )

    # DISPATCH → MONITOR (派发后总是进入监控)
    graph.add_edge("dispatch", "monitor")

    # DELIVER → LEARN_SUCCESS / REVISION / LEARN_FAILURE
    graph.add_conditional_edges(
        "deliver",
        after_deliver,
        {
            "learn_success": "learn_success",
            "revision": "revision",
            "learn_failure": "learn_failure",
        },
    )

    return graph


def compile_brain_graph(checkpointer: AsyncPostgresSaver | None = None):
    """编译 Brain 状态机

    Args:
        checkpointer: 可选的 PostgresSaver 实例，用于持久化状态。
                      如未提供，则使用内存 checkpointer（开发模式）。

    Returns:
        编译好的 CompiledGraph，可调用 invoke/ainvoke/stream/astream
    """
    from swarm.tracing import configure_langsmith

    configure_langsmith()
    graph = build_brain_graph()

    if checkpointer is not None:
        compiled = graph.compile(
            checkpointer=checkpointer,
            interrupt_before=[],  # 不在节点前中断（使用 interrupt() 函数控制）
        )
        logger.info("[COMPILE] Brain graph 已编译 (PostgresSaver checkpointer)")
    else:
        # 开发模式: 使用内存 checkpointer
        import os
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "false")

        from langgraph.checkpoint.memory import MemorySaver

        global _memory_checkpointer
        if _memory_checkpointer is None:
            _memory_checkpointer = MemorySaver()
        compiled = graph.compile(
            checkpointer=_memory_checkpointer,
        )
        logger.info("[COMPILE] Brain graph 已编译 (MemorySaver checkpointer - 开发模式)")

    return compiled


def get_compiled_brain_graph():
    """获取进程内单例 Brain graph（任务 runner 与 API 共享，支持 resume）。

    A1 批1：优先使用已初始化的 PG checkpointer 版（多副本共享、跨副本 resume）；
    若 PG checkpointer 未初始化（未调 init_postgres_checkpointer 或初始化失败），
    回退到 MemorySaver 版（开发/CI/单机开箱即用）。
    """
    global _compiled_brain_graph
    if _compiled_brain_graph is None:
        if _pg_checkpointer is not None:
            _compiled_brain_graph = compile_brain_graph(checkpointer=_pg_checkpointer)
        else:
            _compiled_brain_graph = compile_brain_graph()
    return _compiled_brain_graph


def reset_compiled_brain_graph() -> None:
    """测试用：重置单例"""
    global _compiled_brain_graph, _memory_checkpointer
    _compiled_brain_graph = None
    _memory_checkpointer = None


async def init_postgres_checkpointer(postgres_uri: str | None = None) -> bool:
    """A1 批1：初始化 PG checkpointer 单例（在 FastAPI startup 内调用）。

    关键：连接生命周期 = app 生命周期。用 from_conn_string 的 cm 进入（__aenter__）
    并把 cm + checkpointer 都存为模块单例，shutdown 时 close_postgres_checkpointer
    退出（__aexit__）。绝不在函数作用域内用 `async with`（那样函数返回即关连接——
    这正是原 compile_brain_graph_with_postgres 的 bug）。

    返回 True=PG checkpointer 就绪；False=失败已降级（get_compiled_brain_graph 会用 MemorySaver）。
    """
    global _pg_checkpointer, _pg_checkpointer_cm, _compiled_brain_graph
    if _pg_checkpointer is not None:
        return True
    try:
        config = get_config()
        uri = postgres_uri or config.db.postgres_uri
        cm = AsyncPostgresSaver.from_conn_string(uri)
        checkpointer = await cm.__aenter__()
        await checkpointer.setup()  # 幂等创建 checkpoint 表
        _pg_checkpointer_cm = cm
        _pg_checkpointer = checkpointer
        # 让已编译单例（若已用 MemorySaver 建过）失效，下次取用 PG 版
        _compiled_brain_graph = None
        logger.info("[A1] PG checkpointer 已初始化（跨副本 interrupt/resume 就绪）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[A1] PG checkpointer 初始化失败，降级 MemorySaver（单机/开发模式不受影响）: %s", exc
        )
        _pg_checkpointer = None
        _pg_checkpointer_cm = None
        return False


async def close_postgres_checkpointer() -> None:
    """A1 批1：关闭 PG checkpointer 连接（在 FastAPI shutdown 内调用）。"""
    global _pg_checkpointer, _pg_checkpointer_cm, _compiled_brain_graph
    cm = _pg_checkpointer_cm
    _pg_checkpointer = None
    _pg_checkpointer_cm = None
    _compiled_brain_graph = None
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
            logger.info("[A1] PG checkpointer 连接已关闭")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[A1] 关闭 PG checkpointer 失败: %s", exc)
