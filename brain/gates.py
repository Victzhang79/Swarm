"""交付/计划放行闸门 —— "是否允许 auto_accept 放行" 的单一事实源。

背景（task 37460a5b / 0f93f1fc）：CONFIRM 与 DELIVER 两个节点各自手写 auto_accept
放行逻辑，导致同构 bug "修一个漏一个"：
  - CONFIRM 修了 "非法计划不放行"(P0-3)，DELIVER 却仍无条件 ACCEPT，把 escalate 的
    失败任务当成功放行 → 污染 LEARN_SUCCESS 知识库。

本模块把放行判定收敛为两个纯函数（无副作用、易测、可复用）：
  - can_auto_accept_plan(state)     —— 计划层（CONFIRM 用）
  - can_auto_accept_delivery(state) —— 产出层（DELIVER 用）

设计原则：
  - 单一事实源：所有"能否放行"的判据集中在此，新增交付门也复用，杜绝同构漏修。
  - 语义精确：l3_passed 三态（True/False/None=跳过）——只有显式 False 才算失败，
    跳过(None)不得误判为失败（否则关闭 L3 的项目永远无法 auto_accept）。
  - 返回 (allow, reason)：reason 用于日志与 verification_failure 归因，便于排查。
"""

from __future__ import annotations

from typing import Any


def can_auto_accept_plan(state: dict[str, Any]) -> tuple[bool, str]:
    """CONFIRM 阶段：auto_accept 是否可放行此计划。

    规则：计划自动校验未通过(plan_valid=False) → 不放行(fail-fast)。
    """
    if not state.get("plan_valid", True):
        issues = state.get("plan_validation_issues") or []
        reason = "; ".join(issues) if issues else "计划自动校验未通过"
        return False, f"plan_invalid: {reason}"
    return True, ""


def can_auto_accept_delivery(state: dict[str, Any]) -> tuple[bool, str]:
    """DELIVER 阶段：auto_accept 是否可把产出当"成功"放行。

    任一为真即【拒绝放行】(fail-fast，走 LEARN_FAILURE 学成错误模式)：
      - failure_escalated：子任务重试耗尽已升级人工
      - failed_subtask_ids 非空：仍有未恢复的失败子任务
      - l2_passed 为假：L2 集成验证未通过
      - l3_passed 显式为 False：L3 预发验证失败（None=跳过，不算失败）
      - verification_failure 非空：存在已记录的验证失败来源

    返回 (allow, reason)。reason 同时用作 verification_failure 的归因值。
    """
    if state.get("failure_escalated", False):
        return False, "failure_escalated: 子任务重试耗尽已升级人工"

    failed = state.get("failed_subtask_ids") or []
    if failed:
        return False, f"failed_subtasks: 仍有未恢复的失败子任务 {failed}"

    if not state.get("l2_passed", False):
        return False, "l2_failed: L2 集成验证未通过"

    # l3_passed 三态：None=跳过(不算失败)，False=失败，True=通过
    l3 = state.get("l3_passed", None)
    if l3 is False:
        return False, "l3_failed: L3 预发验证失败"

    vf = state.get("verification_failure")
    if vf:
        return False, f"verification_failure: {vf}"

    return True, ""
