"""Brain Graph — 构建 LangGraph StateGraph，注册节点和边，编译为可执行 graph

状态机流转:
  RECEIVED → ANALYZE → PLAN → VALIDATE_PLAN →
    [CONFIRM (ultra)] → DISPATCH → MONITOR →
      [HAS_FAILURE → HANDLE_FAILURE → DISPATCH |
       HAS_MORE → DISPATCH |
       ALL_DONE → MERGE → VERIFY_L2 → [L2 gate] VERIFY_RUNTIME → [runtime gate] VERIFY_L3 → DELIVER →
         [ACCEPT → LEARN_SUCCESS |
          REVISE  → REVISION → DISPATCH |
          REJECT  → LEARN_FAILURE] → DONE]

条件边:
  - after_validate: plan_valid? → CONFIRM(ultra)/DISPATCH(non-ultra) / PLAN(retry)
  - after_confirm: human_decision? → DISPATCH(accept) / DONE(reject)
  - after_monitor: has_failures? → HANDLE_FAILURE / has_more? → DISPATCH / MERGE
  - after_merge: merge_conflicts? → HANDLE_FAILURE / VERIFY_L2
  - after_verify_l2: l2_passed? → VERIFY_RUNTIME / HANDLE_FAILURE
  - after_verify_runtime: runtime_smoke_passed? → VERIFY_L3(True/None=skipped) / HANDLE_FAILURE(False)
  - after_verify_l3: l3_passed? → DELIVER / HANDLE_FAILURE
  - after_handle_failure: strategy? → DISPATCH(retry) / PLAN(replan) / DELIVER(escalate)
  - after_deliver: human_decision? → LEARN_SUCCESS/REVISION/LEARN_FAILURE
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, StateGraph

from swarm.brain.ingest_node import ingest
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

# T1：对抗验证节点直连模块导入（同 verify_runtime 先例，不经 __init__ re-export）
from swarm.brain.nodes.adversarial import adversarial_verify

# S1-4：verify_runtime 直连模块导入（不经 __init__ re-export——延活转交批对 __init__ 的
# 改动面收敛在 _run_reactor_build_in_sandbox 两处，不动其导出面）。
from swarm.brain.nodes.verify import verify_runtime
from swarm.brain.planning_nodes import (
    assess,
    clarify,
    contract_design,
    detect_stack,
    elaborate,
    review_design,
    tech_design,
)
from swarm.brain.requirements_extract import extract_requirements
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


def _make_checkpoint_serde():
    """round36 #12：给 checkpointer serde 显式登记 swarm.types.* 到 allowed_msgpack_modules。
    否则 LangGraph 反序列化 checkpoint 里的 TaskPlan/SubTask/枚举刷"unregistered type … will be
    blocked in a future version"警告，且未来版本会【直接拒绝反序列化】→ 破坏 interrupt/resume 崩溃
    恢复。动态收集 swarm.types 全部 pydantic/枚举类(自动含未来新增，不写死清单)。取用失败返回 None
    →调用方保留默认 serde(绝不因此破坏 checkpointer 构造)。★证据：round36 全程 unregistered 警告仅
    swarm.types.*（BrainState schema 固定），故显式允许 swarm.types 即完备；builtins/常见类型走 langgraph
    SAFE_MSGPACK_TYPES 恒放行，不会被本允许列表误拦★。"""
    try:
        import enum as _enum
        import inspect as _inspect

        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from pydantic import BaseModel as _BaseModel

        import swarm.types as _stypes
        _classes = [
            _o for _o in vars(_stypes).values()
            if _inspect.isclass(_o) and getattr(_o, "__module__", "") == "swarm.types"
            and (issubclass(_o, _BaseModel) or issubclass(_o, _enum.Enum))
        ]
        if not _classes:
            return None
        return JsonPlusSerializer(allowed_msgpack_modules=_classes)
    except Exception as _exc:  # noqa: BLE001
        logger.warning("[graph] #12 构造 swarm.types serde 允许列表失败(%s)——保留默认 serde", _exc)
        return None


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
    # 复用 effective_complexity 归一：checkpoint resume 后该值会退化成字符串 "ultra"，
    # 裸 `in (枚举,...)` 会漏判 → complex/ultra 误走轻量 PLAN，且 `.value` 抛 AttributeError
    # （task 8537fa5e 同根因）。effective_complexity 统一返回 Complexity 枚举。
    comp = effective_complexity(state)
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
        "；%d 个未坐实 verdict=false 降级 advisory 不阻断" % len(_advisory) if _advisory else "",
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

    # TD2606-A5 补漏：规划 LLM 失败产出的空 scope 兜底假计划【结构上】合法(plan_valid=True)，
    # 非 ULTRA 时旧逻辑直接 validate→dispatch，绕过 confirm 里的 can_auto_accept_plan 拦截 →
    # 空 diff 假 DONE。这里强制走 confirm，让 fail-fast 闸门(auto→REJECT+escalate / 人工→interrupt)生效。
    if state.get("plan_generation_failed"):
        logger.warning("[ROUTE] VALIDATE → CONFIRM (规划生成失败的兜底假计划，须人工/escalate，A5)")
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
        # ── #R13-4 治本：派发面终态熔断（不可满足子任务集的派发面对偶）──
        # 症状(round13 实测)：阶梯三 give-up-preserve 后，剩余子任务【全部依赖已 revert/放弃的
        # 上游 → 永不就绪】，但 dispatch_remaining 非空。旧逻辑"剩余非空即 DISPATCH" → DISPATCH
        # 的 get_dispatch_batch 返回空批、不排空 remaining → MONITOR→DISPATCH 紧致空转
        # 撞 recursion_limit → 整任务 FAILED（而非诚实 PARTIAL）。
        # 治本：DISPATCH 已 await 完本批、此刻无 worker 在飞 → 用【与 DISPATCH 完全相同的就绪
        # 判定】探测；若无一可派发即为不可推进的死结 → 转 MERGE 诚实交付已完成成果(PARTIAL)，
        # 绝不空转。与 HANDLE_FAILURE 侧"不再无界 replan"同源：不可满足集需单一终态出口。
        plan_obj = state.get("plan")
        if plan_obj is not None:
            # 治本 D23：与 DISPATCH 同口径——依赖就绪只认 L1 通过的完成态，
            # 滞留失败结果不得让下游误判可派发。
            from swarm.brain.nodes.shared import completed_l1_ids
            _completed = completed_l1_ids(state.get("subtask_results", {}))
            _abandoned = (set(state.get("abandoned_subtask_ids") or [])
                          | set(state.get("give_up_isolated_ids") or []))
            _mc = get_config().worker.max_concurrent
            _dispatchable = plan_obj.get_dispatch_batch(
                _completed, dispatch_remaining, _mc, _abandoned
            )
            if not _dispatchable:
                logger.warning(
                    "[ROUTE] MONITOR → MERGE (%d 个剩余子任务全不可派发/依赖已放弃 → "
                    "停止空转、PARTIAL 交付已完成成果；#R13-4 派发面终态熔断)",
                    len(dispatch_remaining),
                )
                return "merge"
        logger.info(f"[ROUTE] MONITOR → DISPATCH ({len(dispatch_remaining)} 个剩余)")
        return "dispatch"

    # R65C-T2 修④：完成判据对全计划诚实——remaining==0 可能是«做完了»也可能是
    # «都被放弃了»（round65c：102/107 连坐后 remaining=0，旧标签"全部完成"掩盖计划
    # 覆灭直到 L2 才拦下假交付）。放弃>0 时按三本账 WARNING 留痕，绝不谎报全部完成。
    _plan_final = state.get("plan")
    _aband_final = (set(state.get("abandoned_subtask_ids") or [])
                    | set(state.get("give_up_isolated_ids") or []))
    if _aband_final and _plan_final is not None:
        from swarm.brain.nodes.shared import completed_l1_ids
        _done_n = len(completed_l1_ids(state.get("subtask_results", {})))
        logger.warning(
            "[ROUTE] MONITOR → MERGE (完成 %d + 放弃 %d / 计划 %d —— PARTIAL 交付，"
            "绝非全部完成；放弃清单入终态机读账)",
            _done_n, len(_aband_final), len(_plan_final.subtasks),
        )
        return "merge"
    logger.info("[ROUTE] MONITOR → MERGE (全部完成)")
    return "merge"


def after_adversarial_verify(state: BrainState) -> Literal["merge", "handle_failure"]:
    """ADVERSARIAL_VERIFY 后的路由（T1 对抗验证 gate，三态对齐 after_verify_runtime）:

    - adversarial_verify_passed=False → HANDLE_FAILURE（NAUGHTY 子任务已置 l1_passed=False +
      入 failed_subtask_ids，复用既有重试预算重做——双界收敛：subtask_retry_counts abandon +
      本节点 MAX_ROUNDS 早熔断）
    - True（都过独立双复核）/ None（跳过/降级/升人工，已入 degraded_reasons 可观测）→ MERGE
    """
    if state.get("adversarial_verify_passed") is False:
        logger.warning("[ROUTE] ADVERSARIAL_VERIFY → HANDLE_FAILURE (子任务未过对抗复核，打回重做)")
        return "handle_failure"
    logger.info("[ROUTE] ADVERSARIAL_VERIFY → MERGE (对抗复核通过/跳过)")
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

    - l2_passed=True → 通过出口（标签 "verify_l3"；S1-4 起接线目标是 VERIFY_RUNTIME，
      见 add_conditional_edges 的映射——标签语义=「L2 通过继续验证链」，保持不变以稳住
      既有单测契约 test_p0_path，真实拓扑由 wiring 测试锁定）
    - l2_passed=False → HANDLE_FAILURE（禁止进入 deliver）
    """
    if state.get("l2_passed", False):
        logger.info("[ROUTE] VERIFY_L2 → VERIFY_RUNTIME (L2 通过，先过运行时冒烟)")
        return "verify_l3"

    logger.warning("[ROUTE] VERIFY_L2 → HANDLE_FAILURE (L2 未通过，阻断交付)")
    return "handle_failure"


def after_verify_runtime(state: BrainState) -> Literal["verify_l3", "handle_failure"]:
    """VERIFY_RUNTIME 后的路由（S1-4 运行时冒烟 gate，三态对齐 after_verify_l3 的 P1-12 语义）:

    - runtime_smoke_passed=False → HANDLE_FAILURE（verification_failure="runtime_smoke" 专类归因）
    - True（通过）/ None（skipped，已入 degraded_reasons 可观测留痕）→ VERIFY_L3
    """
    if state.get("runtime_smoke_passed") is False:
        logger.warning("[ROUTE] VERIFY_RUNTIME → HANDLE_FAILURE (运行时冒烟未通过，阻断交付)")
        return "handle_failure"
    if state.get("runtime_smoke_skipped"):
        logger.info("[ROUTE] VERIFY_RUNTIME → VERIFY_L3 (冒烟跳过，degraded 留痕)")
        return "verify_l3"
    logger.info("[ROUTE] VERIFY_RUNTIME → VERIFY_L3 (冒烟通过)")
    return "verify_l3"


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


# R63-T11：LLM 录制作用域——节点分派层统一打标签。
# 录制门控（models/router.py _astream）= env 开 AND _LLM_NODE_CV 非空；此前全仓只有
# 4 个标签点（plan_batch/plan_single/validate_plan/review:{tag}，走 _invoke_llm_abortable），
# tech_design/contract_design/extract_requirements 等直连 llm.ainvoke 的节点全部漏录
# （round63 cassette 实锤 10 行全 plan_batch）。brain LLM streaming=True，ainvoke 也走
# _astream——缺的只是标签，注册层每节点包一次即补全。
# denylist（绝不包装）：dispatch/monitor 会 asyncio.ensure_future spawn worker 任务
# （dispatch.py:610），contextvar 随 spawn 拷贝进 worker 上下文——包了它们 = worker 流量
# 被误录（cassette 铁律：worker 流量不录；worker _run_agent 入口另有 set_llm_node("")
# 双保险）。细粒度标签（_invoke_llm_abortable）在节点标签内层设置，天然嵌套覆盖再还原。
_LLM_NODE_LABEL_DENYLIST = frozenset({"dispatch", "monitor"})


def _labeled_node(name: str, fn):
    import functools
    import inspect

    from swarm.models.router import reset_llm_node, set_llm_node

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def _awrapped(*args, **kwargs):
            tok = set_llm_node(name)
            try:
                return await fn(*args, **kwargs)
            finally:
                reset_llm_node(tok)
        _awrapped.__swarm_llm_node_label__ = name
        return _awrapped

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        tok = set_llm_node(name)
        try:
            return fn(*args, **kwargs)
        finally:
            reset_llm_node(tok)
    _wrapped.__swarm_llm_node_label__ = name
    return _wrapped


def _maybe_labeled(name: str, fn):
    """注册层标签闸：denylist（spawn worker 的节点）原样返回，其余包节点名标签。"""
    if name in _LLM_NODE_LABEL_DENYLIST:
        return fn
    return _labeled_node(name, fn)


# 图节点注册表【单一事实源】（R63-T11）：build_brain_graph 据此注册（经 _maybe_labeled
# 打标签）；test_brain_state_schema / test_ledger_runner_wiring 等冻结面直接 import 消费
# ——此前它们靠正则/AST 扫 add_node 字面量，注册形态一变就静默解析出空集。存【裸】函数，
# 包装在注册时做（schema 测试要按 fn.__name__ 回查源码函数体）。
GRAPH_NODE_REGISTRY: tuple = (
    ("ingest", ingest),        # B 部分：多模态需求摄取（前置于 analyze）
    ("analyze", analyze),
    ("detect_stack", detect_stack),  # 2.7：技术栈/架构识别（plan 前预处理，磁盘 ground truth）
    # Q4 规划子图节点
    ("clarify", clarify),
    ("assess", assess),
    ("tech_design", tech_design),
    ("contract_design", contract_design),  # T1：多模块共享契约（Brain 大模型）
    # S2-2：需求条目结构化（contract_design→plan 之间的轻量纯计算节点）。对称面裁决：
    # 不进 runner._NODE_STATUS_MAP（与 clarify/assess/tech_design/contract_design/elaborate
    # 等规划子图节点同先例——不写任务状态，仍有 brain_node 事件）；非 interrupt、非活跃
    # 执行态之间，checkpoint 风险与 S1-4 结论同级（ACCEPTANCE_DESIGN §4.3）。
    ("extract_requirements", extract_requirements),
    ("review_design", review_design),
    ("elaborate", elaborate),
    ("plan", plan),
    ("validate_plan", validate_plan),
    ("confirm", confirm_plan),
    ("dispatch", dispatch),
    ("monitor", monitor),
    ("adversarial_verify", adversarial_verify),  # T1：对抗验证 stage（MONITOR 全完成→此→MERGE）
    ("handle_failure", handle_failure),
    ("merge", merge),
    ("verify_l2", verify_l2),
    ("verify_runtime", verify_runtime),  # S1-4：运行时冒烟闸门（L2 与 L3 之间）
    ("verify_l3", verify_l3),
    ("deliver", deliver),
    ("revision", revision),
    ("learn_success", learn_success),
    ("learn_failure", learn_failure),
    ("increment_retry", _increment_plan_retry),
)


def build_brain_graph() -> StateGraph:
    """构建 Brain 状态机图

    Returns:
        编译好的 StateGraph，可调用 graph.invoke() / graph.ainvoke()
    """
    graph = StateGraph(BrainState)

    # ── 注册节点 ──（R63-T11：统一经 _maybe_labeled 打 LLM 录制标签，denylist 见上；
    # 节点清单/注释见模块级 GRAPH_NODE_REGISTRY 单一事实源）
    for _name, _fn in GRAPH_NODE_REGISTRY:
        graph.add_node(_name, _maybe_labeled(_name, _fn))

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
    # T1/S2-2：CONTRACT_DESIGN → EXTRACT_REQUIREMENTS → PLAN（契约设计后先做需求条目
    # 结构化再拆解）。此处是所有规划路径的必经汇合点（含 assess simple/medium 绕过
    # tech_design 的路径），故抽取节点挂这里而非 tech_design 之后。⚠️ 禁双边：绝不能
    # 保留旧 add_edge("contract_design", "plan")——静态双边会 fan-out 并行触发
    # （confirm 血案同款，test_requirements_extract_s2_2.py 拓扑断言守护）。
    # replan 环（handle_failure→plan / confirm REVISE→plan）不经过本节点 →
    # requirement_items 不会每轮重抽（ACCEPTANCE_DESIGN §6.4）。
    graph.add_edge("contract_design", "extract_requirements")
    graph.add_edge("extract_requirements", "plan")

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

    # MONITOR → HANDLE_FAILURE / DISPATCH / ADVERSARIAL_VERIFY(全完成成功路径)
    # T1：标签 "merge"（=「全完成，进交付链」）的目标重指 adversarial_verify——全完成后先过
    # 对抗验证复核"声称成功"，再进 merge。标签名保持不变以稳住 after_monitor 语义（含
    # #R13-4 PARTIAL 熔断也返回 "merge"→同经此节点，节点内识别 dispatch_remaining 非空即跳过，
    # 不扰动部分交付）；test_after_monitor_dispatch_fuse.py 断言 after_monitor 返回值不变仍绿。
    graph.add_conditional_edges(
        "monitor",
        after_monitor,
        {
            "handle_failure": "handle_failure",
            "dispatch": "dispatch",
            "merge": "adversarial_verify",
        },
    )

    # ADVERSARIAL_VERIFY → MERGE / HANDLE_FAILURE (T1 对抗验证 gate)
    # ⚠️ 与 confirm/verify_runtime 同款不变量：出口【只】由本条件边决定，绝不可再加
    # add_edge("adversarial_verify", ...) 静态边（fan-out 血案，见 confirm 处注释；
    # test_adversarial_verify_t1.py 拓扑断言守护）。
    graph.add_conditional_edges(
        "adversarial_verify",
        after_adversarial_verify,
        {
            "merge": "merge",
            "handle_failure": "handle_failure",
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

    # VERIFY_L2 → VERIFY_RUNTIME / HANDLE_FAILURE (L2 gate)
    graph.add_conditional_edges(
        "verify_l2",
        after_verify_l2,
        {
            # S1-4：标签 "verify_l3"（=「L2 通过继续验证链」）的目标改指 verify_runtime——
            # L2 通过后先过运行时冒烟，再进 L3。标签名保持不变见 after_verify_l2 docstring。
            "verify_l3": "verify_runtime",
            "handle_failure": "handle_failure",
        },
    )

    # VERIFY_RUNTIME → VERIFY_L3 / HANDLE_FAILURE (runtime gate，S1-4)
    # ⚠️ 与 confirm 同款不变量：verify_runtime 出口【只】由本条件边决定，绝不可再加
    # add_edge("verify_runtime", ...) 静态边（fan-out 血案，见 confirm 处注释；
    # test_verify_runtime_wiring_s1_4.py 拓扑断言守护）。
    graph.add_conditional_edges(
        "verify_runtime",
        after_verify_runtime,
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
            _serde = _make_checkpoint_serde()  # #12：同 PG 路径登记 swarm.types
            if _serde is not None:
                _memory_checkpointer.serde = _serde
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
        # E6（阶段5，登记册 §六）：from_conn_string=单连接（生命周期=app）——PG 闪断后
        # 该连接死透，所有任务的 checkpoint 读写连环失败直到重启。改 AsyncConnectionPool
        # （check=可用性探活+自动重建坏连接，min 1/max 4），运行期自愈；psycopg_pool
        # 不可用时回退旧单连接（行为=旧版，不新增依赖硬要求）。
        cm = None
        try:
            from psycopg_pool import AsyncConnectionPool
            _pool = None
            try:
                _pool = AsyncConnectionPool(
                    uri, min_size=1, max_size=4, open=False,
                    kwargs={"autocommit": True, "prepare_threshold": 0},
                    check=AsyncConnectionPool.check_connection,
                )
                await _pool.open(wait=True, timeout=30)
            except Exception:
                # 5.9 猎手 F6：open 失败的 pool 不自动关闭——后台 worker 无限重连风暴。
                # best-effort close 后原样上抛（外层按 require_pg 决定 fail-fast/降级）。
                if _pool is not None:
                    try:
                        await _pool.close()
                    except Exception:  # noqa: BLE001
                        pass
                raise
            checkpointer = AsyncPostgresSaver(_pool)
            cm = _pool  # close 侧统一处理（pool.close() / cm.__aexit__ 二选一）
            logger.info("[E6] PG checkpointer 使用连接池（min=1,max=4,check=探活自愈）")
        except ImportError:
            cm = AsyncPostgresSaver.from_conn_string(uri)
            checkpointer = await cm.__aenter__()
        await checkpointer.setup()  # 幂等创建 checkpoint 表
        _pg_checkpointer_cm = cm
        _pg_checkpointer = checkpointer
        # round36 #12：登记 swarm.types 到 serde 允许列表（消 unregistered 警告 + 防未来版本拒绝
        # 反序列化 checkpoint 破坏 resume）。取用失败保留默认 serde，不破坏已就绪的 checkpointer。
        _serde = _make_checkpoint_serde()
        if _serde is not None:
            checkpointer.serde = _serde
        # 让已编译单例（若已用 MemorySaver 建过）失效，下次取用 PG 版
        _compiled_brain_graph = None
        logger.info("[A1] PG checkpointer 已初始化（跨副本 interrupt/resume 就绪）")
        return True
    except Exception as exc:  # noqa: BLE001
        # TD2606-B12 / P0-D：降级 MemorySaver 会破坏 interrupt/resume——单进程重启即丢中断
        # checkpoint（任务永久卡 CONFIRMING/DELIVERING，Command(resume) 找不到 snapshot）；
        # 多副本下人工 ACCEPT 路由到另一副本同样找不到 checkpoint。
        # 默认策略：显式设 SWARM_REQUIRE_PG_CHECKPOINTER 则以其为准；未设时【生产环境默认 fail-fast】
        # （单进程也必做），开发/测试保留降级以便无 PG 起服务。
        import os as _os
        _raw = _os.environ.get("SWARM_REQUIRE_PG_CHECKPOINTER")
        if _raw is not None:
            require_pg = _raw.strip().lower() in ("1", "true", "yes")
        else:
            try:
                require_pg = get_config().is_production()
            except Exception:  # noqa: BLE001 — 配置读取失败按保守（非生产）默认，不因此二次崩
                require_pg = False
        if require_pg:
            logger.error(
                "[A1] PG checkpointer 初始化失败且要求强制 PG（生产默认 / SWARM_REQUIRE_PG_CHECKPOINTER）——"
                "拒绝降级 MemorySaver（重启后 interrupt/resume 不可用）: %s", exc
            )
            raise
        logger.warning(
            "[A1] PG checkpointer 初始化失败，降级 MemorySaver（单机/开发不受影响；多副本部署请设"
            " SWARM_REQUIRE_PG_CHECKPOINTER=1 强制 PG 以保跨副本 resume）: %s", exc
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
            # E6：pool 形态（AsyncConnectionPool）用 close()；单连接形态用 cm.__aexit__
            try:
                from psycopg_pool import AsyncConnectionPool as _ACP
            except ImportError:
                _ACP = ()  # type: ignore[assignment]
            if _ACP and isinstance(cm, _ACP):
                await cm.close()
            else:
                await cm.__aexit__(None, None, None)
            logger.info("[A1] PG checkpointer 连接已关闭")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[A1] 关闭 PG checkpointer 失败: %s", exc)
