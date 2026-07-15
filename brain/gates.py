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
    rebased 变更未并入 merged_diff）∪ dispatch_remaining（★D25★：悬空依赖/不可派发子任务经
    #R13-4 熔断进 MERGE，从未执行）。runner 落库、learn 侧 outcome/L6 门槛、统计三处必须同口径
    读此函数，杜绝历史上"learn 侧只看 abandoned 漏 give_up → give_up-only PARTIAL 被学成成功模式"。

    ★#7 治本★：merge_rebase_dropped 此前写进 state 但全仓无消费点 → rebase 超限丢弃的子任务变更
    不进部分交付判据、任务仍标 DONE（静默成功）。纳入此处：即便聚合清单成员由 post-pass reconcile
    据 ground-truth 兜底(多数场景无损)，终态也诚实反映"有 rebased 变更被丢弃,需人工核验"，宁可
    过报 PARTIAL 不可静默 DONE 丢工作。
    """
    _abandoned = state.get("abandoned_subtask_ids") or []
    _given_up = state.get("give_up_isolated_ids") or []
    _rebase_dropped = state.get("merge_rebase_dropped") or []
    # ★治本 D25★：dispatch_remaining 在终态非空 = 悬空依赖/不可派发子任务经 #R13-4 熔断进 MERGE，
    # 从未执行却被静默吞掉。本函数只在终态（runner 落库 / learn outcome）被消费，正常 DONE 时
    # remaining 已排空；此处纳入判据 → 有滞留未执行子任务 → 终态 PARTIAL（不静默 DONE / 不学成成功）。
    _remaining = state.get("dispatch_remaining") or []
    return sorted(set(_abandoned) | set(_given_up) | set(_rebase_dropped) | set(_remaining))


def is_partial_delivery(state: dict[str, Any]) -> bool:
    """终态是否为部分交付（PARTIAL）。见 partial_delivery_ids。"""
    return bool(partial_delivery_ids(state))


def delivery_incomplete(state: dict[str, Any]) -> bool:
    """X-1 残留（外部深审）：交付 apply 全失败/不完整——merged_diff 没（全部）落到项目树 →
    项目实际没拿到本任务的（全部）变更。这是 subtask-id 之外的【任务级】交付失败信号（deliver
    节点写 degraded_reasons），终态判据须纳入，绝不静默 DONE 假成功（DONE 铁律）。

    诚实边界：delivery_commit_failed 【不】入此判据——那种情形 apply 已成功、变更已在工作树
    落盘，只是未提交进 git 历史（/apply-diff、人工 commit 可补），交付本身已达成、非假成功。"""
    _dg = state.get("degraded_reasons") or []
    return any(r in ("delivery_apply_failed", "delivery_apply_incomplete") for r in _dg)


def terminal_status(state: dict[str, Any]) -> str:
    """终态状态单一裁决：有部分交付子任务 或 任务级交付失败 → PARTIAL，否则 DONE。

    单一事实源，runner 落库据此判 DONE/PARTIAL。X-1 残留治本把【任务级交付失败】
    （delivery_incomplete，apply 没落进项目树）与既有【子任务级部分交付】（partial_delivery_ids）
    并入同一判据，杜绝"子任务全成功但产物没交付 → 静默 DONE 假成功"。"""
    return "PARTIAL" if (partial_delivery_ids(state) or delivery_incomplete(state)) else "DONE"


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

    # 阶段0 复核 R1（2026-07-09）：plan_batch_failed 专属归因必须先于通用 plan_invalid——
    # A4 让失败模块轮 plan_valid=False，重试耗尽进 confirm 时若先撞通用分支，round29 专门
    # 建的归因（"误标 plan_invalid 会污染 L5 错题"）被架空、escalate 标记丢失。
    plan_batch_failed = state.get("plan_batch_failed_modules") or []
    if plan_batch_failed:
        _pb_names = [m.get("name", "?") for m in plan_batch_failed if isinstance(m, dict)]
        _pb_files = sum(int(m.get("files") or 0) for m in plan_batch_failed if isinstance(m, dict))
        return False, (
            f"plan_batch_failed: {len(plan_batch_failed)} 个模块分解失败 {_pb_names}"
            f"（共 {_pb_files} 个规划文件未纳入计划）——计划范围残缺，不得静默 auto_accept，须人工介入"
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

    # round29 真因4（W1.1 的 PLAN-BATCH 对等物）判定已上移到 plan_valid 之前（复核 R1）。
    return True, ""


def can_auto_accept_delivery(state: dict[str, Any]) -> tuple[bool, str]:
    """DELIVER 阶段：auto_accept 是否可把产出当"成功"放行。

    任一为真即【拒绝放行】(fail-fast，走 LEARN_FAILURE 学成错误模式)：
      - failure_escalated：子任务重试耗尽已升级人工
      - failed_subtask_ids 非空：仍有未恢复的失败子任务
      - l2_passed 为假：L2 集成验证未通过
      - l3_passed 显式为 False：L3 预发验证失败（None=跳过，不算失败）
      - runtime_smoke_passed 显式为 False：运行时冒烟失败（None=跳过，不算失败；S1-6）
      - acceptance_passed 显式为 False：验收断言失败（None=跳过，不算失败；S2-6）
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

    # S1-6：runtime 冒烟三态（对齐 l3 语义 + 上方 :119 "专类先判、如实归因"先例）：
    # 仅显式 False 阻断；None=跳过不算失败（skipped 已由 degraded_reasons 可观测，
    # should_write_success 据 degraded 另拦 L6，不会学成成功模式）；True=通过不阻断。
    # S2 复核 F5：runtime 失败通道是复用面——verify 的 acceptance/migration 失败也走
    # _runtime_failure_state 置 runtime_smoke_passed=False（ACCEPTANCE_DESIGN 定案4）。
    # 拒因文案必须按 runtime_smoke_details.classification 分型如实归因：断言失败时
    # 应用【明明已启动】，谎称"启动/探活失败"会把人工审核/学习面引向错误根因。
    rt = state.get("runtime_smoke_passed", None)
    if rt is False:
        _rt_details = state.get("runtime_smoke_details")
        _rt_class = str(_rt_details.get("classification") or "") \
            if isinstance(_rt_details, dict) else ""
        if _rt_class == "acceptance_failed":
            return False, (
                "acceptance_failed: 验收断言未通过（应用已启动，接口行为不符预期，"
                "非启动/探活失败）"
            )
        if _rt_class == "migration_failed":
            return False, (
                "migration_failed: migration 验证未通过（确定性 SQL/migration 执行失败，"
                "非启动/探活失败）"
            )
        return False, "runtime_smoke_failed: 运行时冒烟未通过（应用启动/探活失败，非 L2 编译失败）"

    # S2-6：acceptance 三态（镜像上方 runtime 三态先例，判序=runtime 之后、verification_failure
    # 兜底之前）：仅显式 False 阻断；None=跳过不算失败（all_manual/tool_missing 等 skipped
    # 已由 degraded_reasons 可观测，should_write_success 据 degraded 另拦 L6）；
    # True=通过不阻断；旧 checkpoint 缺键=未接线，不阻断。
    # F5 复核注：常态下不可达——verify 的 acceptance 失败路径同时置 runtime_smoke_passed=False，
    # 上方 rt 分支已按 classification 给出 acceptance 专类文案。本分支保留作纵深防御：
    # 仅在 acceptance_passed=False 而 runtime_smoke_passed 缺失/非 False 的异常形态
    # （手工构造 state/未来新写者）下兜底，杜绝断言失败被静默放行。
    acc = state.get("acceptance_passed", None)
    if acc is False:
        return False, (
            "acceptance_failed: 验收断言未通过（应用已启动但接口行为不符预期，"
            "非启动/探活失败）"
        )

    # ★G12（Task#9 审计⑥）★ baseline_covered 被【运行时断言证伪】→ 无条件硬拦 auto_accept
    # （不受 SWARM_BASELINE_STRICT_GATE 默认关影响）：申报"存量已满足"但其断言【已执行且 fail、
    # 无任何 pass】= 谎报，无棕地鉴权墙借口（鉴权墙下无法核实的走下方 unverified 通道、仍受默认关
    # 权衡）。与 runtime/acceptance "显式失败即拦" 同构；证伪的假 DONE 绝不能自动放行、漏需求前滚。
    _contra = [str(d) for d in (state.get("degraded_reasons") or [])
               if str(d).startswith("baseline_covered:contradicted")]
    if _contra:
        return False, (
            f"baseline_contradicted: 存量「已满足」申报被运行时断言证伪（已执行且未过）"
            f"——非「无法核实」而是谎报，拒绝 auto_accept 交人工：{_contra[0][:200]}"
        )

    # R31 hunter F1 收紧阀（默认关，开启需运维/用户拍板）：baseline_covered 申报存在
    # 未经【已执行且 pass】断言验证的条目（鉴权类断言恒 manual / 冒烟 skip 均属此形态）
    # → 拒绝 auto_accept 交人工。消费 verify 写入的同一条 degraded 留痕（不另算一份事实）。
    # 默认关的权衡：棕地任务的诚实申报常无法自动核实（鉴权墙），默认硬拦会把合法交付
    # 全数打失败——默认走"degraded 挡 L6 假学习 + deliver/confirm payload 人工可见"通道。
    import os as _os
    if _os.environ.get("SWARM_BASELINE_STRICT_GATE", "0").strip().lower() in (
            "1", "true", "yes", "on"):
        _unv = [str(d) for d in (state.get("degraded_reasons") or [])
                if str(d).startswith("baseline_covered:unverified")]
        if _unv:
            return False, (
                f"baseline_unverified: 存量申报未经运行时验证（严格闸开启）：{_unv[0][:200]}"
            )

    # T1 对抗验证（hunter F1 整改）：对抗复核【不收敛达上限】= 有负面证据未解决（子任务
    # 反复被独立复核判 FAIL 却修不好）→ 硬拦 auto_accept 交人工。与 runtime/acceptance 的
    # "显式失败即拦"同构。注意【只拦 unconverged】：reviewer 不可用/漏审覆盖不全（缺复核、
    # 无负面证据）走 degraded+挡 L6 通道（should_write_success 已据 blocking_degraded_reasons
    # 拦 L6 假学习），不在此硬拦——否则 provider 挂时会 strand 全部交付（对齐 runtime_smoke
    # skip 不硬拦的既有哲学，adversarial.py 节点 docstring 有分型论证）。
    _adv = [str(d) for d in (state.get("degraded_reasons") or [])
            if str(d).startswith("adversarial_verify_unconverged")]
    if _adv:
        return False, (
            f"adversarial_verify_unconverged: 对抗复核多轮未收敛（子任务反复未过独立复核），"
            f"需人工介入：{_adv[0][:200]}"
        )

    vf = state.get("verification_failure")
    if vf:
        return False, f"verification_failure: {vf}"

    return True, ""
