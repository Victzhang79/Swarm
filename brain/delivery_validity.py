"""交付真实性判定的纯函数。

本模块只读当前 Brain state，不做路由或副作用。它把“历史缺项账”和“当前轮已验证、
可交付的缺项”分开，避免人工 ACCEPT 把旧 checkpoint 或未验证产物包装成成功。
"""

from __future__ import annotations

from typing import Any


_PARTIAL_KEYS = (
    "abandoned_subtask_ids",
    "give_up_isolated_ids",
    "merge_rebase_dropped",
    "dispatch_remaining",
    "partial_salvage_ids",
)


def current_plan_ids(state: dict[str, Any]) -> set[str]:
    plan = state.get("plan")
    subtasks = (
        plan.get("subtasks") if isinstance(plan, dict) else getattr(plan, "subtasks", None)
    ) or []
    return {
        str(item.get("id") if isinstance(item, dict) else getattr(item, "id", ""))
        for item in subtasks
    } - {""}


def current_plan_success_ids(
    state: dict[str, Any], failed_ids: list[str], results: dict[str, Any]
) -> list[str]:
    """返回当前 plan 内真实 L1 通过的非失败兄弟；缺 plan 时 fail-closed。"""
    plan_ids = current_plan_ids(state)
    failed = {str(fid) for fid in failed_ids}
    if not failed or not failed.issubset(plan_ids):
        return []
    from swarm.brain.nodes.shared import l1_passed

    return sorted(
        str(sid)
        for sid, output in results.items()
        if str(sid) in plan_ids and str(sid) not in failed and l1_passed(output)
    )


def partial_merge_result(
    state: dict[str, Any],
    failed_ids: list[str],
    results: dict[str, Any],
    **extra: object,
) -> dict[str, Any]:
    """把成功兄弟交给 MERGE；失败产物出账，待验证后才可形成 PARTIAL。"""
    failed = {str(fid) for fid in failed_ids}
    plan_ids = current_plan_ids(state)
    from swarm.brain.nodes.shared import l1_passed

    retained = {
        sid: output
        for sid, output in results.items()
        if str(sid) in plan_ids and str(sid) not in failed and l1_passed(output)
    }
    return {
        **extra,
        "subtask_results": retained,
        "failed_subtask_ids": [],
        "partial_salvage_ids": sorted(set(failed_ids)),
        "failure_escalated": False,
        "failure_strategy": "partial_merge",
        "l2_passed": None,
        "verification_failure": None,
    }


def raw_partial_delivery_ids(state: dict[str, Any]) -> list[str]:
    ids: set[str] = set()
    for key in _PARTIAL_KEYS:
        ids.update(str(item) for item in (state.get(key) or []) if item)
    return sorted(ids)


def hard_delivery_failure_reason(state: dict[str, Any]) -> str:
    """返回不可由人工 ACCEPT 覆盖的当前轮失败原因。"""
    if state.get("delivery_finalization_failed") is True:
        return "delivery_finalization_failed"
    if state.get("clarify_blocked_by_facts") is True:
        return "clarification_required"
    if state.get("plan_valid") is not True:
        return (
            "plan_invalid" if state.get("plan_valid") is False
            else "plan_validation_incomplete"
        )
    if state.get("failure_escalated") is True:
        return "failure_escalated"
    if state.get("failed_subtask_ids"):
        return "failed_subtasks"
    if state.get("verification_failure"):
        return "verification_failure"
    for key in ("l2_passed", "runtime_smoke_passed", "l3_passed", "acceptance_passed"):
        if state.get(key) is False:
            return key.removesuffix("_passed") + "_failed"
    return ""


def delivery_validation_chain_complete(state: dict[str, Any]) -> bool:
    """接受交付所需的当前轮正向证据；明确 skip 是诚实结论而非成功伪装。"""
    return (
        state.get("l2_passed") is True
        and (
            state.get("runtime_smoke_passed") is True
            or state.get("runtime_smoke_skipped") is True
        )
        and (state.get("l3_passed") is True or state.get("l3_skipped") is True)
        # verify_runtime 的每个当前轮出口都会显式写此三态键：True=通过、False=失败、
        # None=本轮明确跳过。旧 checkpoint/绕过节点造成的键缺失不能与显式 skip 坍缩。
        and "acceptance_passed" in state
        and (
            state.get("acceptance_passed") is True
            or state.get("acceptance_passed") is None
        )
    )


def verified_partial_delivery_ids(state: dict[str, Any]) -> list[str]:
    """当前 plan 内有真实成功产物且完成验证链的部分交付账。"""
    partial = set(raw_partial_delivery_ids(state))
    if not partial or hard_delivery_failure_reason(state):
        return []
    if not delivery_validation_chain_complete(state):
        return []
    if not str(state.get("merged_diff") or "").strip():
        return []

    plan_ids = current_plan_ids(state)
    if not plan_ids or not partial.issubset(plan_ids):
        return []
    results = state.get("subtask_results")
    if not isinstance(results, dict):
        return []

    # 延迟导入，避免 gates -> delivery_validity -> nodes.__init__ 的初始化环。
    from swarm.brain.nodes.shared import l1_passed

    has_deliverable = any(
        str(sid) in plan_ids and str(sid) not in partial and l1_passed(output)
        for sid, output in results.items()
    )
    return sorted(partial) if has_deliverable else []


def carry_partial_salvage(
    state: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """按恢复处方保留、并集或核销当前轮 salvage 账。"""
    old = {str(item) for item in (state.get("partial_salvage_ids") or []) if item}
    strategy = result.get("failure_strategy")
    if strategy == "replan":
        carried: set[str] = set()
    elif strategy == "partial_merge":
        carried = old | {
            str(item) for item in (result.get("partial_salvage_ids") or []) if item
        }
    else:
        requeued = {str(item) for item in (result.get("dispatch_remaining") or []) if item}
        carried = old - requeued
    return {**result, "partial_salvage_ids": sorted(carried)}


def partial_merge_provenance(
    state: dict[str, Any], results: dict[str, Any]
) -> tuple[dict[str, Any], set[str], bool, list[str]]:
    """剔除计划外旧结果，并返回当前待抢救账与 plan 缺失事实。"""
    pending = {str(item) for item in (state.get("partial_salvage_ids") or []) if item}
    plan_ids = current_plan_ids(state)
    plan_missing = bool(not plan_ids and (pending or results))
    # 过滤是 MERGE 的当前-plan 身份咽喉，不是 partial salvage 的附属行为。普通 retry
    # 同样可能从旧 checkpoint 播种 L1 成功结果；只在 pending 非空时过滤会把旧轮 diff
    # 静默并入当前交付。当前 plan 非空时一律按它裁剪，且把丢弃项返回给调用方留机读账。
    out_of_plan = sorted(
        str(sid)
        for sid in results
        if plan_missing or (plan_ids and str(sid) not in plan_ids)
    )
    filtered = {} if plan_missing else (
        {sid: value for sid, value in results.items() if str(sid) in plan_ids}
        if out_of_plan else dict(results)
    )
    return filtered, pending, plan_missing, out_of_plan


def partial_merge_failure_patch(
    *, plan_missing: bool, pending: set[str], merged_diff: str
) -> dict[str, Any]:
    if not (plan_missing or (pending and not merged_diff.strip())):
        return {}
    return {
        "failure_escalated": True,
        "failure_strategy": "escalate",
        "l2_passed": False,
        "verification_failure": (
            (
                "partial_salvage_plan_missing" if pending else "merge_plan_missing"
            ) if plan_missing else "partial_salvage_empty_merge"
        ),
    }
