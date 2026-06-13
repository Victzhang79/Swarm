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
    elaborate,
    review_design,
    tech_design,
)
from swarm.brain.ingest_node import ingest
from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.types import Complexity, HumanDecision

logger = logging.getLogger(__name__)

# 最大计划重试次数
MAX_PLAN_RETRY = 3

# 进程内单例 — 共享 checkpointer，支持跨 HTTP 请求 interrupt/resume
_compiled_brain_graph = None
_memory_checkpointer = None


# ══════════════════════════════════════════════
# 条件边函数
# ══════════════════════════════════════════════


def after_analyze(state: BrainState) -> Literal["clarify", "plan"]:
    """ANALYZE 后路由（Q4 规划子图入口）：
    - needs_clarify(非微任务&非自动化) → CLARIFY（进交互式渐进规划）
    - 微任务 / 自动化 / 不需澄清 → PLAN（直达，极速/轻量）
    """
    if state.get("needs_clarify") and not state.get("is_micro_task"):
        logger.info("[ROUTE] ANALYZE → CLARIFY (进规划子图)")
        return "clarify"
    logger.info("[ROUTE] ANALYZE → PLAN (微任务/自动化/直达)")
    return "plan"


def after_clarify(state: BrainState) -> Literal["clarify", "assess"]:
    """CLARIFY 后路由（多轮循环）：
    - clarify_done → ASSESS（澄清完成，定级）
    - 否则 → CLARIFY（继续下一轮，LLM 再评估是否还要问）
    """
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


def after_validate(state: BrainState) -> Literal["confirm", "plan", "dispatch"]:
    """VALIDATE_PLAN 后的路由:

    - plan_valid=False & 重试次数未达上限 → 重新 PLAN
    - plan_valid=True & complexity=ultra → CONFIRM (人工确认)
    - plan_valid=True & complexity!=ultra → 直接 DISPATCH
    """
    plan_valid = state.get("plan_valid", False)
    retry_count = state.get("plan_retry_count", 0)
    complexity = state.get("complexity", Complexity.MEDIUM)

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


def after_merge(state: BrainState) -> Literal["handle_failure", "verify_l2", "dispatch"]:
    """MERGE 后的路由:

    - merge_conflicts 非空 → HANDLE_FAILURE（failed_subtask_ids 已由 merge 节点填充）
    - rebase_subtask_ids 非空（无硬冲突）→ DISPATCH（rebase 子任务已加入 dispatch_remaining，需重跑）
    - 无冲突无 rebase → VERIFY_L2
    """
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
    # Q4 规划子图节点
    graph.add_node("clarify", clarify)
    graph.add_node("assess", assess)
    graph.add_node("tech_design", tech_design)
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
    graph.add_edge("confirm", "dispatch")       # confirm 后实际路由在 after_confirm 条件边
    graph.add_edge("revision", "dispatch")
    graph.add_edge("learn_success", END)
    graph.add_edge("learn_failure", END)

    # ── Q4 规划子图边 ──
    # ANALYZE →[条件] CLARIFY / PLAN
    graph.add_conditional_edges(
        "analyze",
        after_analyze,
        {"clarify": "clarify", "plan": "plan"},
    )
    # CLARIFY ⟲[条件] CLARIFY(多轮) / ASSESS
    graph.add_conditional_edges(
        "clarify",
        after_clarify,
        {"clarify": "clarify", "assess": "assess"},
    )
    # ASSESS →[条件] TECH_DESIGN / PLAN
    graph.add_conditional_edges(
        "assess",
        after_assess,
        {"tech_design": "tech_design", "plan": "plan"},
    )
    # TECH_DESIGN → REVIEW_DESIGN
    graph.add_edge("tech_design", "review_design")
    # REVIEW_DESIGN →[条件] TECH_DESIGN(打回) / PLAN(通过)
    graph.add_conditional_edges(
        "review_design",
        after_review,
        {"tech_design": "tech_design", "plan": "plan"},
    )

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
    """获取进程内单例 Brain graph（任务 runner 与 API 共享，支持 resume）"""
    global _compiled_brain_graph
    if _compiled_brain_graph is None:
        _compiled_brain_graph = compile_brain_graph()
    return _compiled_brain_graph


def reset_compiled_brain_graph() -> None:
    """测试用：重置单例"""
    global _compiled_brain_graph, _memory_checkpointer
    _compiled_brain_graph = None
    _memory_checkpointer = None


async def compile_brain_graph_with_postgres(
    postgres_uri: str | None = None,
):
    """使用 PostgresSaver 编译 Brain 状态机（异步）

    Args:
        postgres_uri: PostgreSQL 连接串，默认从配置读取

    Returns:
        编译好的 CompiledGraph
    """
    config = get_config()
    uri = postgres_uri or config.db.postgres_uri

    async with AsyncPostgresSaver.from_conn_string(uri) as checkpointer:
        await checkpointer.setup()  # 创建 checkpoint 表
        return compile_brain_graph(checkpointer=checkpointer)
