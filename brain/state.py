"""BrainState — LangGraph 状态机的完整状态定义"""

from __future__ import annotations

from typing import TypedDict

from swarm.types import (
    Complexity,
    HumanDecision,
    KnowledgeContext,
    TaskPlan,
    WorkerOutput,
)


class BrainState(TypedDict, total=False):
    """Swarm Brain 状态机的完整状态。

    每个字段都可通过 LangGraph 的 reduce 注解进行更新，
    total=False 表示所有字段均为可选（初始状态不必包含全部字段）。

    生命周期: RECEIVED → ANALYZE → PLAN → VALIDATE_PLAN →
              [CONFIRM(ultra)] → DISPATCH → MONITOR → MERGE →
              VERIFY_L2 → DELIVER →
              [ACCEPT→LEARN_SUCCESS | REVISE→REVISION→DISPATCH | REJECT→LEARN_FAILURE] → DONE
    """

    # ─── 任务标识 ───
    task_id: str                        # 唯一任务 ID
    task_description: str               # 原始任务描述
    project_id: str                     # 所属项目 ID
    user_id: str                        # 任务发起人（L1 画像）
    user_profile: dict                  # L1 画像 JSON
    user_profile_prompt_brain: str      # 格式化后注入 Brain LLM
    user_profile_prompt_worker: str     # 格式化后注入 Worker LLM

    # ─── 分析阶段 ───
    complexity: Complexity              # LLM 判定的复杂度 (simple/medium/complex/ultra)
    knowledge_context: KnowledgeContext  # 知识检索结果
    affected_files: list[str]           # 检索定位文件（plan 覆盖校验）
    session_metadata: dict              # L0 会话元数据（ephemeral）
    recent_task_summaries: list[dict]    # L2 近期摘要（只读）

    # ─── 计划阶段 ───
    plan: TaskPlan                      # 拆解后的子任务 DAG
    plan_valid: bool                    # 计划验证结果
    plan_retry_count: int               # 计划重试次数
    plan_validation_issues: list[str]   # PlanValidator 问题列表
    shared_contract: dict               # Brain 级共享契约（来自 plan）

    # ─── 执行阶段 ───
    subtask_results: dict[str, WorkerOutput]  # 已完成的子任务输出，key=subtask_id
    dispatch_remaining: list[str]       # 尚未派发/等待中的子任务 ID 列表
    failed_subtask_ids: list[str]       # 失败的子任务 ID 列表
    failure_strategy: str               # handle_failure 决策: retry|retry_alternate|replan|escalate
    use_alternate_model: bool           # retry_alternate 时使用备选模型
    failure_escalated: bool             # escalate 时标记需人工介入
    subtask_retry_counts: dict[str, int]  # 每个子任务的累计重试次数（确定性递进升级）

    # ─── 合并 & 验证 ───
    merged_diff: str                    # 合并后的完整 diff
    merge_conflicts: list[dict]         # merge 冲突详情（file_path, subtask_ids, message）
    rebase_subtask_ids: list[str]       # rebase 重生成子任务 ID（3-way 失败后选一方 base，另一方重新生成）
    l2_passed: bool                     # L2 集成测试是否通过
    l3_passed: bool | None              # L3 预发验证结果（None=跳过）
    l3_skipped: bool                    # L3 是否跳过
    l3_message: str                     # L3 验证说明
    verification_failure: str | None    # l2 / l3 等验证失败来源

    # ─── L3 滑动窗口（任务执行期上下文）───
    context_log: list[dict]             # 上下文事件 log
    context_summary: str                # 被压缩掉的历史摘要
    context_token_estimate: int         # 估算 token 数

    # ─── 人工决策 ───
    human_decision: HumanDecision       # ACCEPT / REVISE / REJECT

    # ─── 修订 ───
    revision_feedback: str              # 人类修订反馈

    # ─── 学习 ───
    learned: bool                       # 是否已完成学习步骤
    learn_summary: str                  # 学习摘要（成功模式或错误模式）

    # ─── API/自动化模式 ───
    auto_accept: bool                   # API 模式下自动接受 interrupt 节点

    # ═══ Q4 交互式渐进规划 Agent（规划子图，纯加法）═══
    # ─── 微任务极速通道(D) ───
    is_micro_task: bool                 # 单点/低风险/无架构影响（如"按钮黄→绿"）→ 跳过澄清/方案/明细
    # ─── 澄清阶段（多轮自适应 ≤5）───
    ambiguity_score: float              # analyze 初判的信息缺口程度 0-1
    needs_clarify: bool                 # analyze 初判：是否需进入澄清流程
    clarify_round: int                  # 当前澄清轮次（0 起）
    clarify_history: list[dict]         # [{round, questions:[{q,why,default_if_skipped}], answers}]
    clarify_summary: str                # 多轮澄清的滚动摘要（C：防上下文堆积）
    clarify_done: bool                  # 信息已足够 / 达上限 / 用户跳过
    # ─── 澄清后定级(Q2 复杂度后置)───
    assessed_complexity: Complexity     # 澄清后基于完整信息+知识库定的真复杂度（覆盖 analyze 初判）
    # ─── 技术方案 + 评审(Q5/Q6/B)───
    tech_design: dict                   # {stack, architecture, data_model_diagram, flow_diagram, risks, notes, acceptance, change_impact, maintainability, comment_requirements}
    shared_contract_draft: dict         # 接口先行(B)：API schema / 数据模型，供并行子任务作稳定前置
    design_review: dict                 # {decision: approve|reject, feedback, reject_count}
    # ─── 渐进明细(两层)───
    plan_milestones: list[dict]         # L0 骨架：[{goal, modules, risks}]
    plan_elaborated: bool               # 是否已从骨架展开为子任务 DAG
    # ─── 上下文预算 + INVEST 自检(Q7/A)───
    oversized_subtask_ids: list[str]    # 预估上下文/产出超预算、拆不下的子任务（需人工提示）
    invest_fail_count: int              # INVEST 自检未过被打回再拆的次数
