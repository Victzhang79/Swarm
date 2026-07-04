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


def partial_delivery_ids(state: dict[str, Any]) -> list[str]:
    """部分交付的子任务 ID（单一事实源，去重保序）。

    终态 PARTIAL 判据 = abandoned（重试耗尽连坐放弃）∪ give_up（阶梯三保 build 放弃：
    本地树已清/打桩）∪ rebase_dropped（★B6 复核 #7★：merge rebase 达上限被丢弃的子任务——其
    rebased 变更未并入 merged_diff）。runner 落库、learn 侧 outcome/L6 门槛、统计三处必须同口径
    读此函数，杜绝历史上"learn 侧只看 abandoned 漏 give_up → give_up-only PARTIAL 被学成成功模式"。

    ★#7 治本★：merge_rebase_dropped 此前写进 state 但全仓无消费点 → rebase 超限丢弃的子任务变更
    不进部分交付判据、任务仍标 DONE（静默成功）。纳入此处：即便聚合清单成员由 post-pass reconcile
    据 ground-truth 兜底(多数场景无损)，终态也诚实反映"有 rebased 变更被丢弃,需人工核验"，宁可
    过报 PARTIAL 不可静默 DONE 丢工作。
    """
    _abandoned = state.get("abandoned_subtask_ids") or []
    _given_up = state.get("give_up_isolated_ids") or []
    _rebase_dropped = state.get("merge_rebase_dropped") or []
    return sorted(set(_abandoned) | set(_given_up) | set(_rebase_dropped))


def is_partial_delivery(state: dict[str, Any]) -> bool:
    """终态是否为部分交付（PARTIAL）。见 partial_delivery_ids。"""
    return bool(partial_delivery_ids(state))


def can_auto_accept_plan(state: dict[str, Any]) -> tuple[bool, str]:
    """CONFIRM 阶段：auto_accept 是否可放行此计划。

    任一为真即【拒绝放行】(fail-fast)：
      - plan_valid=False：计划自动校验未通过。
      - tech_design_failed_modules 非空（W1.1）：ultra 两阶段 tech_design 里有模块的
        phase-2 设计生成失败 → 这些模块文件丢失、file_plan 不完整。绝不能让 auto_accept
        把"交付不完整"的任务静默放行当成功，须升级人工审核残缺的设计。
    """
    # TD2606-A5：规划 LLM 失败产出的空 scope「无验证」兜底假计划。validate_plan 可能把这种
    # 单子任务结构判"合法"(plan_valid=True) → 旧逻辑会静默 auto_accept → dispatch → 空 diff →
    # 假 DONE。专用标记 fail-fast 拦下，不得静默放行，须人工介入。
    if state.get("plan_generation_failed"):
        return False, (
            "plan_generation_failed: 规划 LLM 失败，产出的是空 scope 兜底假计划"
            "（Worker 必失败），不得静默 auto_accept，须人工介入"
        )

    if state.get("tech_design_generation_failed"):
        return False, (
            "tech_design_generation_failed: 技术方案整体生成失败（LLM 异常），"
            "file_plan 为空、方案为占位，不得静默 auto_accept，须人工介入"
        )

    # #6：纵深防御——plan_valid 缺省判 False（validate 节点正常总会显式置位；缺失=未经校验，
    # 保守拒绝放行，不假定合法）。
    if not state.get("plan_valid", False):
        issues = state.get("plan_validation_issues") or []
        reason = "; ".join(issues) if issues else "计划自动校验未通过/未执行"
        return False, f"plan_invalid: {reason}"

    failed_modules = state.get("tech_design_failed_modules") or []
    if failed_modules:
        names = [m.get("name", "?") for m in failed_modules if isinstance(m, dict)]
        return False, (
            f"tech_design_incomplete: {len(failed_modules)} 个模块设计生成失败 {names}"
            "——file_plan 不完整，不得静默 auto_accept，须人工介入"
        )
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
    # 治本(task 661ecacb)：虚假前提阻断（TECH_DESIGN 事实核验 → CLARIFY → DELIVER）必须【最先】
    # 判定并【如实归因】。否则会落到下面的 l2_passed=False 分支，把"需澄清"误报成 "l2_failed:
    # L2 集成验证未通过"——而该任务【从未派发、从未跑过 L2】，归因错误且污染 L5 错题（学成不存在
    # 的 L2 失败）。此处给准确原因 + 可操作指引（用 --no-auto-accept 重跑并在澄清处补全事实）。
    if state.get("clarify_blocked_by_facts"):
        summary = (state.get("clarify_summary") or "需求存在虚假前提，需人工澄清").strip()
        return False, (
            "clarification_required: 检出虚假前提，需人工澄清后再执行"
            "（请用 --no-auto-accept 重跑并在澄清处补全事实）。详情：" + summary[:400]
        )

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
