"""任务状态的单一事实源（SSOT）。

此前活跃/终态集合散在两处各自定义（`brain/runner._ACTIVE_DB_STATUSES` 与
`project/store._TERMINAL_STATUSES`），会漂移。崩溃恢复对账（P0-A）需要把"进行中"任务
按语义分三类处置，故在此把三子集收敛到一处：

- ACTIVE_EXECUTION_STATES：执行已真正起步、占用外部副作用资源（沙箱/工作树/模块锁）。
  崩溃后这些资源已死，PG checkpoint 只存图状态、不含外部态，无中途续跑入口 → fail-closed。
  （SUBMITTED 例外语义见下方注释。）
- INTERRUPT_SUSPENDED_STATES：在人工审核处 interrupt 挂起，无在飞外部工作，checkpoint 是
  完整安全续跑点 → 保留，等人工经 Command(resume) 继续。
- TERMINAL_STATES：终态，不再变化。

此模块【不得 import 任何 swarm 内部模块】，保持为无依赖叶子，供 runner / store / scheduler
共同引用而不引入循环依赖。
"""

from __future__ import annotations

# 执行已起步的活跃态。SUBMITTED 虽在此集合（"进行中"，可取消），但语义上是"已入队、
# 尚未创建任何外部资源"——崩溃恢复时不 fail-closed，而是重新入队自动恢复（见 P0-A reconcile）。
ACTIVE_EXECUTION_STATES = frozenset({
    "SUBMITTED",
    "ANALYZING",
    "PLANNING",
    "VALIDATING_PLAN",
    "DISPATCHING",
    "MONITORING",
    "HANDLING_FAILURE",
    "MERGING",
    "VERIFYING_L2",
    "VERIFYING_RUNTIME",  # S1-4 运行时冒烟闸门：活跃执行态（占沙箱），崩溃恢复 fail-closed
    "VERIFYING_L3",
    "IN_REVISION",
    "LEARNING_SUCCESS",
    "LEARNING_FAILURE",
})

# 人工闸 interrupt 挂起态。checkpoint 是安全续跑点，崩溃后保留、等人工审批 resume。
INTERRUPT_SUSPENDED_STATES = frozenset({
    "CONFIRMING",
    "DELIVERING",
    "CLARIFYING",
    "DESIGN_REVIEW",
})

# 终态：DONE/FAILED/CANCELLED/PARTIAL。
TERMINAL_STATES = frozenset({
    "DONE",
    "FAILED",
    "CANCELLED",
    "PARTIAL",
})

# 全部"进行中"状态 = 活跃执行态 ∪ 中断挂起态 = 崩溃后可能 orphaned 的候选全集。
# （runner._ACTIVE_DB_STATUSES 引用此值 → CLARIFYING/DESIGN_REVIEW 纳入孤儿/可取消判定，
#  修好 P0-D cancel/delete 死区。）
ACTIVE_DB_STATUSES = ACTIVE_EXECUTION_STATES | INTERRUPT_SUSPENDED_STATES

# 审批端点（approve/revise/reject）作用的人工闸态子集：计划确认 + 结果审核。
# P1-A：审批前置态校验 + 原子认领只在这两态放行（clarify→CLARIFYING、review-design→DESIGN_REVIEW
#  各自单态，端点内直接用字面量）。
PLAN_RESULT_REVIEW_STATES = frozenset({"CONFIRMING", "DELIVERING"})
