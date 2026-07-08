"""BrainState — LangGraph 状态机的完整状态定义"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from swarm.types import (
    Complexity,
    HumanDecision,
    KnowledgeContext,
    TaskPlan,
    WorkerOutput,
)


def _merge_degraded_reasons(
    old: list[str] | None, new: list[str] | None
) -> list[str]:
    """LangGraph reducer for ``degraded_reasons`` — append + dedup, order-preserving.

    Why this is the ONLY reduced field in BrainState:
    降级原因是「累积事实」（多个节点各自留痕，谁都不该覆盖谁），所以需要 reducer
    把每个节点返回的更新【合并】进当前列表，而非 last-write-wins 覆盖。其余字段需要
    replace/reset 语义（如 plan、failed_subtask_ids 在 replan/重试时要整体替换），
    因此【绝不能】加 reducer。

    合并规则：返回 ``old`` 后追加 ``new`` 中尚未出现的条目（去重、保序）。
    任一侧为 None 容错为 []。

    ── ALWAYS-EMIT 结构契约（重要，新增节点必读）──
    引入 reducer 后，节点返回的 ``{"degraded_reasons": X}`` 会被【合并】进当前态而非
    替换。因此：
      1. 写降级原因的节点，返回【完整合并列表】或【仅增量】都正确——dedup 保证不重不漏。
         本仓库现状是返回完整列表（已 dedup），reducer 再幂等合并一次，安全。
      2. 处于「环路源头」的节点（merge / handle_failure(dispatch) / validate_plan），
         无论成功/干净路径，都必须显式 emit 自己的【路由控制键】（如 merge 的
         ``rebase_subtask_ids``、dispatch 的 ``failed_subtask_ids``），不能依赖上一轮
         的残留——否则重入时会读到过期值导致错误路由。该契约由
         test/test_brainstate_always_emit.py 以源码静态断言锁定，防回归。
    """
    merged = list(old or [])
    for item in (new or []):
        if item not in merged:
            merged.append(item)
    return merged


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
    # LLM 降级可见性（audit #12/#13）：analyze/plan 等节点在 LLM 不可用而走静默兜底
    # （复杂度回退 MEDIUM、空 scope 兜底 plan）时追加原因，透传到交付/通知，让人工
    # 审核能看见"本任务经历了降级"，而非误以为系统正常。
    degraded_reasons: Annotated[list[str], _merge_degraded_reasons]

    # ─── 计划阶段 ───
    plan: TaskPlan                      # 拆解后的子任务 DAG
    plan_valid: bool                    # 计划验证结果
    plan_retry_count: int               # 计划重试次数
    plan_validation_issues: list[str]   # PlanValidator 问题列表
    # D09：VALIDATE_PLAN 失败原因回灌 PLAN——校验失败时写入本轮 issues 摘要，PLAN 重试时读它注入
    # LLM prompt（否则 after_validate 失败→increment_retry→plan 是【盲重试】，LLM 看不到上轮为何被否
    # →原样重生成同样坏计划→烧光 MAX_PLAN_RETRY→confirm/REJECT）。校验通过时清空，防跨轮粘滞。
    plan_validation_feedback: str
    shared_contract: dict               # Brain 级共享契约（来自 plan）
    # D10：PLAN 节点对 plan.parallel_groups 剔除悬空引用后，把修剪结果同步写回 state 顶层
    # （dedupe/replan 改了 subtasks 集合时 groups 必须跟着改）。LangGraph 未声明键会被静默
    # 丢弃——不声明则修剪结果蒸发，plan_validator 仍读到悬空组硬失败。
    parallel_groups: list[list[str]]

    # ─── 执行阶段 ───
    subtask_results: dict[str, WorkerOutput]  # 已完成的子任务输出，key=subtask_id
    dispatch_remaining: list[str]       # 尚未派发/等待中的子任务 ID 列表
    failed_subtask_ids: list[str]       # 失败的子任务 ID 列表
    failure_strategy: str               # handle_failure 决策: retry|retry_alternate|replan|escalate
    use_alternate_model: bool           # retry_alternate 时使用备选模型
    failure_escalated: bool             # escalate 时标记需人工介入
    subtask_force_strong: dict[str, bool]  # FINDING-12：拒答/步数耗尽的子任务，重试强制走最强模型+更多步数
    abandoned_subtask_ids: list[str]    # 部分交付：重试耗尽被放弃的子任务（+其依赖者），任务终态 PARTIAL 而非灭全部
    give_up_isolated_ids: list[str]     # 卡死子任务恢复阶梯·阶梯三：保 build 放弃的子任务（本地树已 revert/打桩清干净，build 不被毒）——终态 PARTIAL，诚实列明需人工补完
    # ★B6 #7★ merge rebase 达上限被丢弃 rebased 变更的子任务——纳入 partial_delivery_ids，终态 PARTIAL
    # 而非静默 DONE。复核 L-2：既已决定 PARTIAL-vs-DONE，用 append+dedup reducer（而非 last-writer-wins），
    # 未来若有并行分支也写此键不会静默丢早先条目。
    merge_rebase_dropped: Annotated[list[str], _merge_degraded_reasons]
    subtask_retry_counts: dict[str, int]  # 每个子任务的累计【capability】重试次数（换模型/升级阶梯）
    subtask_redecompose_count: dict[str, int]  # 卡死子任务恢复阶梯·阶梯二：定点拆小次数（有界，每子任务≤1）
    subtask_transient_counts: dict[str, int]  # P2：每个子任务的累计【瞬时】退避重试次数（与 capability 配额隔离）
    replan_count: int                   # P0-2：replan 累计次数（熔断上限，防无限重规划）
    replan_feedback: str                # P0-2：上轮失败根因，replan 重入时注入 PLAN 供 LLM 规避
    targeted_recovery_count: int        # P0-B(f9e38dae)：定向恢复累计次数——round29 遗漏项#2 起仅作遥测，熔断改用 targeted_recovery_counts（按子任务）
    targeted_recovery_counts: dict[str, int]  # round29 遗漏项#2：定向恢复次数【按子任务】熔断 {sid: n}——旧任务级全局计数会被先失败者用光、饿死后续同类受害者（d37a52a3 st-25 从未拿到 pom 写权即"已达上限"空烧）。A2 缺依赖 + 序修复阶梯共用此表，同子任务环安全语义不变
    targeted_recovery: bool             # P0-B：本轮走了定向恢复（补 pom 写权+只重派失败，不进 PLAN/不清全表）
    confirm_reason: str                 # P0-3：confirm 进入原因(validation_failed|ultra|manual_confirm)

    # ─── 合并 & 验证 ───
    merged_diff: str                    # 合并后的完整 diff
    merge_conflicts: list[dict]         # merge 冲突详情（file_path, subtask_ids, message）
    rebase_subtask_ids: list[str]       # rebase 重生成子任务 ID（3-way 失败后选一方 base，另一方重新生成）
    # audit #30：rebase 不计入 subtask_retry_counts（策略性重生成≠失败重试），但需独立上限
    # 防 rebase→fail→rebase 无限循环。记录每个子任务的累计 rebase 次数。
    subtask_rebase_counts: dict[str, int]
    l2_targeted: bool                   # TD2606-B8：L2 失败已归因到具体子任务（定向重做，保留成功兄弟）
    l2_passed: bool                     # L2 集成测试是否通过
    l3_passed: bool | None              # L3 预发验证结果（None=跳过）
    l3_skipped: bool                    # L3 是否跳过
    l3_message: str                     # L3 验证说明
    l3_branch: str                      # N-04：verify_l3 实际推送的分支，供 learn_success MR 指向正确分支
    verification_failure: str | None    # l2 / l3 / runtime_smoke 等验证失败来源（handle_failure 专类分支据此归因）

    # ═══ S1-4 运行时冒烟闸门（VERIFY_RUNTIME，docs/RUNTIME_SMOKE_DESIGN.md §4）═══
    # 为什么必须声明：LangGraph 未声明键=【静默丢弃】（本文件下方 schema 补全段实证）——不声明则
    # verify_runtime 写的三态结论全部蒸发，after_verify_runtime 路由永远读 None、失败回灌成死功能。
    # 为什么全部 last-write-wins 无 reducer（本文件顶部原则）：replan/重试重入 verify_runtime 时
    # 必须【整体替换】上一轮结论而非累积合并——加 reducer 会让旧轮失败结论粘滞误导路由。
    runtime_smoke_passed: bool | None   # 三态路由键（None=跳过≠失败，对齐 l3_passed 语义）：仅 False 进 handle_failure
    runtime_smoke_skipped: bool         # skipped 可观测锚点：gates/交付摘要据此区分「没跑」和「跑过没过」，绝不静默
    runtime_smoke_message: str          # 如实说明（通过/失败形态/为何跳过），透传 deliver/通知/学习
    runtime_smoke_details: dict[str, Any]  # 三分类判据留痕（classification/log_tail/探活序列）：task#20 失败归因回灌 + UI 排障的数据源
    runtime_smoke_sandbox_id: str       # L2 编译沙箱延活转交的 sid（进程内 manager._instances registry 查键；仅诊断留痕不作恢复依据——沙箱对象不可序列化进 PG checkpoint）；verify_runtime 消费后清空防跨轮粘滞
    migration_verify_passed: bool | None   # migration 执行验证三态（task#21 写入；先声明——否则未来节点写它会被静默丢成死功能）
    migration_verify_details: dict[str, Any]  # migration 验证细节留痕（同上，声明先行）

    # ═══ S2 验收断言与需求条目（docs/ACCEPTANCE_DESIGN.md BrainState 新键清单）═══
    # 声明先行铁律（S1 migration 键先例）：LangGraph 未声明键=静默丢弃。四键全部
    # last-write-wins 无 reducer（均非累积事实：replan/design 重做需整体替换，加 reducer
    # 会让旧轮结论粘滞误导路由）。skipped/降级可观测走现成 degraded_reasons reducer。
    requirement_items: list[dict]       # S2-2：结构化需求条目 [{id: req-<sha1[:8]>, text, kind, source_quote, source, source_truncated?}]，extract_requirements 节点写（contract_design→plan 之间）；防幻觉=source_quote 回指原文确定性校验，抽取失败如实降级 []
    baseline_covered: list[dict]        # R31-1 T1：PLAN 申报的"存量已满足"条目 [{id, reason}]，plan 节点 always-emit（未申报=[]，last-write-wins 防跨重试粘滞）；★独立键绝不挂 TaskPlan 字段——plan 变异重构造路径（batched/resplit/revision/水平合并）天然碰不到，结构性防 v0.9.23 F1"变异路径丢字段"类复发★；覆盖校验=covers∪合法申报，申报条目仍生成验收断言（假申报→acceptance_failed 兜底）
    acceptance_assertions: list[dict]   # S2：任务级验收断言 spec [{id, req_id, kind:"http_probe", request, expect, auth}]（task#25 acceptance_spec 写入；声明先行）
    acceptance_passed: bool | None      # S2：验收断言三态结论（None=跳过≠失败，对齐 l3_passed/migration_verify_passed）——verify_runtime accept phase 写入（task#25/26），本批只声明不写入
    acceptance_details: dict[str, Any]  # S2：断言逐条 verdict+证据留痕（deliver 展示/失败回灌数据源）——同上，本批只声明不写入

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
    project_stack: dict                 # 技术栈/架构画像(detect_stack 预处理产，磁盘 ground truth)：{frontend,frontend_kind,backend,build,confidence,evidence,...}，权威优先于需求文档框架假设，供 tech_design/plan/worker 统一消费
    shared_contract_draft: dict         # 接口先行(B)：API schema / 数据模型，供并行子任务作稳定前置
    tech_design_fact_issues: list       # 事实核验问题（虚假前提）：[{claim, verdict(false/already_exists/uncertain), detail, suggestion}]
    tech_design_file_plan: list         # 文件级技术方案：[{path, action(create/modify), responsibility, depends_on}]，喂给 PLAN 定 scope
    tech_design_failed_modules: list    # W1.1：ultra 两阶段 tech_design 中 phase-2 LLM 失败的模块 [{name, idx, reason}]——这些模块文件丢失，file_plan 不完整，绝不能静默 auto_accept 成功，须升级人工
    plan_batch_failed_modules: list     # round29 真因4：PLAN-BATCH 分批拆解失败的模块 [{name, files, reason}]——整模块子任务蒸发=交付范围残缺（d37a52a3 'system-enhance' 14 文件实证），can_auto_accept_plan 据此 fail-fast 升人工；plan 节点 always-emit（成功清空不粘滞）
    clarify_blocked_by_facts: bool      # 虚假前提阻断：auto 模式也不能用默认假设硬跑，需人工澄清/终止
    design_review: dict                 # {decision: approve|reject, feedback, reject_count}
    # ─── 渐进明细(两层)───
    plan_milestones: list[dict]         # L0 骨架：[{goal, modules, risks}]
    plan_elaborated: bool               # 是否已从骨架展开为子任务 DAG
    # ─── 上下文预算 + INVEST 自检(Q7/A)───
    oversized_subtask_ids: list[str]    # 预估上下文/产出超预算、拆不下的子任务（需人工提示）
    invest_fail_count: int              # INVEST 自检未过被打回再拆的次数

    # ═══ schema 补全（CODEWALK 根因A）：以下键早已是实际读写通道但此前未声明——实证
    # （批4a toy StateGraph）LangGraph 对未声明键是【静默丢弃】而非宽容存活：节点返回与
    # initial_state 两路都建不了 channel → 这些链路整体失活（base_commit 恒 None 走回退、
    # plan_generation_failed 闸门死代码、deliver_auto_reject_reason 永不触发）。
    # 补声明=激活链路；一致性由 test_brain_state_schema.py AST 扫描锁定。═══
    base_commit: str                    # runner 任务启动时记录的项目基线 commit（merge/rebase/worker base_ref 锚点）
    plan_generation_failed: bool        # PLAN LLM 拆解失败走兜底计划的标记 → can_auto_accept_plan fail-fast 拦截
    tech_design_generation_failed: bool  # F7(round28)：tech_design 整体 LLM 失败→file_plan 为空/方案占位的 fail-fast 标记 → can_auto_accept_plan(gates.py:66) 拦下升级人工。此前未声明→LangGraph 静默丢→闸门死代码（与 plan_generation_failed 同类，AST 测试原 glob 只扫 brain/nodes/ 漏了 brain/planning_nodes.py 才放过）
    deliver_auto_reject_reason: str     # DELIVER 自动拒绝原因（runner 读取回写任务态/前端展示）
    l2_details: dict[str, Any]          # VERIFY_L2 结构化细节（apply/build/test 输出摘要）

    # ═══ 多模态需求摄取层（设计 v3 B 部分，纯加法，前置于 analyze）═══
    uploaded_files: list[str]           # 任务创建时上传的文件路径（绝对路径，任务专属目录）
    ingest_draft: str                   # 摄取层产出的需求草稿（文档解析+图片理解合并）
    ingest_vision_pending: list[dict]   # 待人工确认的 AI 视觉理解 [{filename, understanding, confirmed}]
    ingest_done: bool                   # 摄取是否已完成（幂等：避免重复摄取）
    ingest_errors: list[str]            # 摄取过程中的非致命错误（单文件失败等）
    auto_confirm_vision: bool           # 用户勾选「模型自行确认」→ 跳过图片理解的人工确认（B.2）


# ─────────────────────────────────────────────────────────────
# 复杂度真值入口（单一来源，杜绝散落读法导致的分歧）
# ─────────────────────────────────────────────────────────────
def effective_complexity(state: BrainState) -> Complexity:
    """复杂度的唯一真值入口：澄清后定级(assess) 优先，回退 analyze 初判，再兜底 MEDIUM。

    背景（修复 12.3）：`complexity` 由 analyze 节点写入（初评），`assessed_complexity`
    由 clarify→assess 节点在澄清后重新定级写入。若任务在澄清后才升/降级，所有"读初评
    complexity"的路由/跳过逻辑都会基于过期判断 —— 典型后果是澄清后升到 ultra 的任务
    漏掉 CONFIRM 人工确认闸门，或仍走 SIMPLE 快速路径跳过校验/集成验证。

    所有需要"当前生效复杂度"的判断点都应调用本函数，而非各自 `state.get(...)`，
    以保证语义一致、避免未来新增节点再次踩坑。

    归一：checkpoint resume 后枚举会反序列化成字符串("ultra")——本函数统一返回 Complexity
    枚举，杜绝下游 `== Complexity.X` 静默错配 / `.value` 抛 AttributeError（task 8537fa5e 真因）。
    """
    comp = state.get("assessed_complexity") or state.get("complexity", Complexity.MEDIUM)
    if isinstance(comp, Complexity):
        return comp
    try:
        return Complexity(str(comp).lower())
    except ValueError:
        return Complexity.MEDIUM
