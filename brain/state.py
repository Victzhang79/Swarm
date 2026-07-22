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
    plan_validation_warnings: list[str]  # G3-2：规划期软警告（规则5 落空/C1 无主符号）机读面
    # D09：VALIDATE_PLAN 失败原因回灌 PLAN——校验失败时写入本轮 issues 摘要，PLAN 重试时读它注入
    # LLM prompt（否则 after_validate 失败→increment_retry→plan 是【盲重试】，LLM 看不到上轮为何被否
    # →原样重生成同样坏计划→烧光 MAX_PLAN_RETRY→confirm/REJECT）。校验通过时清空，防跨轮粘滞。
    plan_validation_feedback: str
    # F10（阶段3.7）：validate LLM 软校验的 plan 结构签名（不含 id）——重试轮签名一致
    # 则跳过软校验（此前每轮必烧 ~120K 字符且结果丢弃）。last-write-wins 每轮整体替换。
    plan_soft_review_sig: str
    # R64-T3：G1 结构性违例签名 {"sig": [...], "retry": N}（绑定 retry 轮次）——连续两轮
    # 同签名=全量重产也无法收敛（round64：反馈注入 plan_batch 但其 schema 无 module 字段
    # +P4 禁改前缀，结构性无法执行）→ 熔断顶格 retry 直接 CONFIRM，省 33min 重产。
    # retry 绑定天然免疫跨 replan 周期的陈旧残留（新周期至少获得一次带反馈重试）。
    plan_validation_prev_structural: dict
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
    # 阶段3.9 复核 H-F7/R-F1（CONFIRMED）：替代全局 bool use_alternate_model——决策针对
    # 【失败撮】却记全局，dispatch 对失败子任务降优先级使首批大概率是无关新前沿，
    # 消费即清把 alternate 路由送给无关批、真正重试者反拿主力模型。按子任务记账：
    # handle_failure 写 {sid: True}，dispatch 逐子任务消费（派出即从表中清除）。
    subtask_use_alternate: dict[str, bool]
    failure_escalated: bool             # escalate 时标记需人工介入
    subtask_force_strong: dict[str, bool]  # FINDING-12：拒答/步数耗尽的子任务，重试强制走最强模型+更多步数
    abandoned_subtask_ids: list[str]    # 部分交付：重试耗尽被放弃的子任务（+其依赖者），任务终态 PARTIAL 而非灭全部
    give_up_isolated_ids: list[str]     # 卡死子任务恢复阶梯·阶梯三：保 build 放弃的子任务（本地树已 revert/打桩清干净，build 不被毒）——终态 PARTIAL，诚实列明需人工补完
    # ★B6 #7★ merge rebase 达上限被丢弃 rebased 变更的子任务——纳入 partial_delivery_ids，终态 PARTIAL
    # 而非静默 DONE。复核 L-2：既已决定 PARTIAL-vs-DONE，用 append+dedup reducer（而非 last-writer-wins），
    # 未来若有并行分支也写此键不会静默丢早先条目。
    merge_rebase_dropped: Annotated[list[str], _merge_degraded_reasons]
    subtask_retry_counts: dict[str, int]  # 每个子任务的累计【capability】重试次数（换模型/升级阶梯）
    contract_retry_counts: dict[str, int]  # D13（阶段6）：契约偏离重试独立表——横切集成面失败不挤兑个体 capability 配额
    subtask_redecompose_count: dict[str, int]  # 卡死子任务恢复阶梯·阶梯二：定点拆小次数（有界，每子任务≤1）
    subtask_transient_counts: dict[str, int]  # P2：每个子任务的累计【瞬时】退避重试次数（与 capability 配额隔离）
    subtask_block_signatures: dict[str, dict]  # B2（round38c）：BLOCKED 失败指纹 {sid: {"sig": str, "count": int}}——同签名重派短路（禁同输入白跑整条阶梯）
    exec_fail_sig_counts: dict[str, int]  # #108 DR-PM66-A2：执行期【签名keyed】不收敛熔断 {归一失败签名: 全任务累计出现次数}——per-id 计数器被 ID 增殖(st-32→st-32-1→…)架空，本表按失败签名跨 id 累计，≥K 强制 give-up（fail-honest PARTIAL）
    subtask_scope_amend_counts: dict[str, int]  # B3-2/B4-2（round38c）：外科 scope 修正次数（补 create_files/异议改名，每子任务≤1，防修正震荡）
    contract_failed_modules: list[str]  # C4-8（round38c）：共享契约缺片模块名（CONTRACT_MODULE 放弃后机读可见；成功路径清空）
    replan_count: int                   # P0-2：replan 累计次数（熔断上限，防无限重规划）
    baseline_repair_rounds: int         # T3（round63）：基线锚修复扫描累计轮次——阻断在基线模块（HEAD 自带、plan 无生产者）时的修复臂计数，封顶 max_retries 防"修了又被投毒"无界循环；耗尽即判死锁连坐放弃（PARTIAL）
    replan_feedback: str                # P0-2：上轮失败根因，replan 重入时注入 PLAN 供 LLM 规避
    targeted_recovery_count: int        # P0-B(f9e38dae)：定向恢复累计次数——round29 遗漏项#2 起仅作遥测，熔断改用 targeted_recovery_counts（按子任务）
    targeted_recovery_counts: dict[str, int]  # round29 遗漏项#2：定向恢复次数【按子任务】熔断 {sid: n}——旧任务级全局计数会被先失败者用光、饿死后续同类受害者（d37a52a3 st-25 从未拿到 pom 写权即"已达上限"空烧）。A2 缺依赖 + 序修复阶梯共用此表，同子任务环安全语义不变
    # （3.8 生命周期收敛删除 targeted_recovery：写后全仓零读点零清点的死键——一次定向恢复后
    #  永久 True 纯误导；遥测由 targeted_recovery_count/counts 承担。）
    confirm_reason: str                 # P0-3：confirm 进入原因(validation_failed|ultra|manual_confirm)；REVISE 开新轮时由 revision 清空（防终态归因读到陈旧进闸原因）

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
    runtime_smoke_last_signature: str    # T4 无进展 plateau 检测（ECC §D）：上一轮 runtime 冒烟失败签名(classification|归因子任务集排序)，handle_failure 跨轮比对——连续两轮同签名=无进展；默认仅观测留痕，SWARM_RUNTIME_SMOKE_PLATEAU_STRICT=1 才短路提前 escalate。last-write-wins（每轮整体替换上轮签名，绝不累积）
    migration_verify_passed: bool | None   # migration 执行验证三态（task#21 写入；先声明——否则未来节点写它会被静默丢成死功能）
    migration_verify_details: dict[str, Any]  # migration 验证细节留痕（同上，声明先行）

    # ═══ S2 验收断言与需求条目（docs/ACCEPTANCE_DESIGN.md BrainState 新键清单）═══
    # 声明先行铁律（S1 migration 键先例）：LangGraph 未声明键=静默丢弃。四键全部
    # last-write-wins 无 reducer（均非累积事实：replan/design 重做需整体替换，加 reducer
    # 会让旧轮结论粘滞误导路由）。skipped/降级可观测走现成 degraded_reasons reducer。
    requirement_items: list[dict]       # S2-2：结构化需求条目 [{id: req-<sha1[:8]>, text, kind, source_quote, source, source_truncated?}]，extract_requirements 节点写（contract_design→plan 之间）；防幻觉=source_quote 回指原文确定性校验，抽取失败如实降级 []
    plan_batch_cache: dict              # R32-1 U2：ULTRA 分批的成功批缓存 {签名: {module, subtasks, baseline}}，plan 节点 always-emit（非分批路径恒 {}，last-write-wins 覆写防陈旧）；只在"上一轮有失败批"的补齐型重试复用——上一轮批全成的纯覆盖分歧重试绝不吃缓存（否则 T3 增量修补/申报永远无法生效）
    baseline_covered: list[dict]        # R31-1 T1：PLAN 申报的"存量已满足"条目 [{id, reason}]，plan 节点 always-emit（未申报=[]，last-write-wins 防跨重试粘滞）；★独立键绝不挂 TaskPlan 字段——plan 变异重构造路径（batched/resplit/revision/水平合并）天然碰不到，结构性防 v0.9.23 F1"变异路径丢字段"类复发★；覆盖校验=covers∪合法申报，申报条目仍生成验收断言（假申报→acceptance_failed 兜底）
    # 阶段3.1 单调合同脊柱（登记册 §八 阶段3，2026-07-09）：曾在【任意】规划轮达成覆盖的
    # req id 全集（covers∪合法 baseline 申报，validate_plan 每轮 emit 本轮覆盖集）。
    # reducer=append+dedup ——【结构性单调不减】：节点 emit 子集也不会让水位缩水（round37
    # 实证覆盖 16→2 的倒退此前只有 log 可见）。消费：validate_plan 相对水位丢失→结构化
    # 回灌 D09 feedback + 覆盖闸通过仍倒退时硬 invalid（A6 degraded 放行后 load-bearing
    # 硬地板）。陈旧 id（清单外）在比对时被过滤，永不误杀；本键绝不需要清空（任务级单调）。
    coverage_watermark: Annotated[list[str], _merge_degraded_reasons]
    # R65E9-T1（round65e9 FAILED@PLAN 三路定案·下游机制根）：被证据闸判为【假 baseline_covered】
    # （申报存量但基线符号/文件索引零命中）的 req id 全集，单调累积（append+dedup reducer）。
    # 死因：baseline_covered=last-write-wins/feedback=oneshot（记忆缺失）→被拒 req 陷 limbo（非
    # covered 非 unplanned）→L2 file-replan 跳过→planner 每 retry 重 declare 同一 req→死钉耗尽
    # 3-retry→FAILED@PLAN（req-feaae262 Redis 诊断，基线真无 Redis）。治：validate_plan 每轮 emit
    # 本轮被拒 baseline id → 单调累积；build_coverage_matrix 无条件把 pinned id 踢出合法 baseline →
    # 落 uncovered → 逼 planner 建 covers 子任务（进 L2 replan），且 PLAN 提示告知不得再 declare。
    # 陈旧 id（清单外）比对时过滤，永不误杀；任务级单调，绝不需清空。
    baseline_ineligible_reqs: Annotated[list[str], _merge_degraded_reasons]
    # 阶段3.9 复核 H-F5（CONFIRMED）：A6 缺口 degraded 放行的残差 req id——独立
    # last-write-wins 键（不进 append-only degraded_reasons：那里无人能清，缺口后来被
    # 补齐仍永久拦 L6+deliver 展示陈旧缺口）。validate_plan 真放行时 emit：gap 放行=
    # 残差覆写、全覆盖=[] 清空。消费：should_write_success（非空拦 L6 假成功学习）+
    # deliver payload（人工可见）。
    coverage_gap_residual: list[str]
    acceptance_assertions: list[dict]   # S2：任务级验收断言 spec [{id, req_id, kind:"http_probe", request, expect, auth}]（task#25 acceptance_spec 写入；声明先行）
    acceptance_passed: bool | None      # S2：验收断言三态结论（None=跳过≠失败，对齐 l3_passed/migration_verify_passed）——verify_runtime accept phase 写入（task#25/26），本批只声明不写入
    acceptance_details: dict[str, Any]  # S2：断言逐条 verdict+证据留痕（deliver 展示/失败回灌数据源）——同上，本批只声明不写入

    # ═══ T1 对抗验证 stage（ADVERSARIAL_VERIFY，ECC §B santa-method 移植；MONITOR 全完成→此→MERGE）═══
    # 声明先行铁律（同 S1/S2 键）：LangGraph 未声明键=静默丢弃。全部 last-write-wins 无 reducer
    # （非累积事实：每轮整体替换上轮结论，加 reducer 会让旧轮结论粘滞误导路由）。降级走现成 degraded_reasons。
    adversarial_verify_passed: bool | None  # 三态路由键：False→handle_failure(打回)；True(都过)/None(跳过/降级/升人工)→merge。对齐 runtime_smoke_passed 语义
    adversarial_verify_round: int       # 不收敛熔断计数（santa MAX_ITER）：NAUGHTY 打回一次+1，达 SWARM_ADVERSARIAL_MAX_ROUNDS 短路 escalate，绝不无界烧 token；always-emit
    adversarial_verified_ids: list[str]  # 已过独立双复核的子任务 ID（下轮跳过不重审=省成本）；always-emit（跳过路径回传原值防跨轮粘滞）
    adversarial_verify_details: dict[str, Any]  # NAUGHTY 逐子任务评语留痕（failure_scenario 集）：deliver 展示/失败回灌数据源
    adversarial_verify_message: str     # 如实说明（通过/打回/为何跳过/升人工），透传 deliver/通知，绝不静默

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
    tech_design_zero_change_modules: list  # R67B-T2：STAGE2 显式申报零改造的既有基线模块 [{name, idx}]——0 文件是诚实申报非丢失（与 failed 三分账），confirm 人工闸/交付对账据此定向核对
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
    l2_missing_fp_history: list[str]    # R46-3：L2 契约缺失符号指纹连击史（三连不变→跳过 D5 归因直接升级，杜绝同缺口空转重跑 L2；指纹变化即重置）
    subtask_dispatch_totals: dict[str, int]  # A2(r48c)：终身派发计数（按 id 单调、绝不签名剪枝）——handle_failure 硬熔断兜底，治 retry_counts 被 scope 加宽/replan 改签名重置后的无界重派
    subtask_alternate_ever_used: dict[str, bool]  # #33-CRITICAL：曾在备选模型上试过的 sid（持久·只增账本）——dispatch 消费 subtask_use_alternate 会派出即清，无法辨"从未换过"；本表单调累积、绝不 dispatch 消费，仅 replan 签名剪枝（同 subtask_dispatch_totals 纪律），闸1 据它判"病灶从未换备选"防无界重触发
    redispatch_wait_windows: dict[str, int]  # R65TR-T3 P2：重派承诺账龄 {sid: 连续未兑现窗口数}——曾派发过却持续未被选中者逐窗口计龄，阈值整倍数 WARNING 点名未满足依赖（#71 终态账提前到飞行中）；被选中/离开 remaining 即清

    # ═══ 多模态需求摄取层（设计 v3 B 部分，纯加法，前置于 analyze）═══
    uploaded_files: list[str]           # 任务创建时上传的文件路径（绝对路径，任务专属目录）
    ingest_draft: str                   # 摄取层产出的需求草稿（文档解析+图片理解合并）
    ingest_vision_pending: list[dict]   # 待人工确认的 AI 视觉理解 [{filename, understanding, confirmed}]
    ingest_done: bool                   # 摄取是否已完成（幂等：避免重复摄取）
    ingest_errors: list[str]            # 摄取过程中的非致命错误（单文件失败等）
    auto_confirm_vision: bool           # 用户勾选「模型自行确认」→ 跳过图片理解的人工确认（B.2）


# ─────────────────────────────────────────────────────────────
# 阶段3.8（2026-07-09 登记册 §八）：记账键生命周期登记表——单一事实源。
# 历史 bug 类=「仅条件写、无人清」的粘滞键（replan_feedback/failure_escalated/
# l2_targeted/merge_conflicts/use_alternate_model/adversarial_verify_round…同一族）。
# 登记表把每个记账/控制键的生命周期显式化，test_phase3_state_lifecycle.py 锁定：
# 新增记账键必须登记，登记键必须有对应类别的清点/重置纪律。
#
# 类别：
#   oneshot   一次性消费键：写→指定消费点消费后必须清（例：replan_feedback 由 PLAN
#             成功产出清；l2_targeted 由 handle_failure l2 三出口清）。
#   round     轮次键：每轮/每决策必须整体替换或 always-emit（不靠残留；例：
#             failure_strategy 每次 handle_failure 整体替换；subtask_use_alternate 由
#             dispatch 按子任务消费——派出即清该 sid）。
#   monotonic 单调累积键：只增不减（reducer 或语义保证；per-subtask dict 账表须在
#             replan 时按签名剪枝——D08 纪律，见 _surgical_replan_reset）。
#   terminal  任务级常量/终态键：写一次不清合法（终态归因/锚点）。
# ─────────────────────────────────────────────────────────────
ACCOUNTING_KEY_LIFECYCLE: dict[str, str] = {
    # 规划闸
    "plan_retry_count": "round",
    "plan_validation_issues": "round",
    "plan_validation_warnings": "round",  # G3-2：last-write-wins（每轮重算）
    "plan_validation_feedback": "oneshot",
    "plan_batch_cache": "round",
    "plan_batch_failed_modules": "round",
    "baseline_covered": "round",
    "coverage_watermark": "monotonic",
    "baseline_ineligible_reqs": "monotonic",  # R65E9-T1：拒掉的假 baseline_covered id 单调累积
    "coverage_gap_residual": "round",   # A6 残差 last-write-wins：gap 放行覆写/全覆盖清空（3.9 H-F5）
    "plan_soft_review_sig": "round",    # 只在真放行时 emit，否决轮发空串（3.9 H-F6/R-F5）
    "plan_validation_prev_structural": "round",  # R64-T3：G1 失败轮整体替换；retry 绑定免疫陈旧残留
    "plan_generation_failed": "round",
    "oversized_subtask_ids": "round",
    "invest_fail_count": "round",
    # 失败/重试
    "replan_count": "monotonic",
    "baseline_repair_rounds": "monotonic",  # T3：修复臂轮次熔断账本——剪了=封顶被绕（同 subtask_dispatch_totals 理由）
    "replan_feedback": "oneshot",
    "failed_subtask_ids": "round",
    "failure_strategy": "round",
    "subtask_use_alternate": "round",   # 按子任务消费：派出即清该 sid（3.9 H-F7/R-F1，替代全局 bool）
    "failure_escalated": "round",
    "subtask_force_strong": "monotonic",   # D08 签名剪枝（3.8 补）
    "subtask_retry_counts": "monotonic",   # D08 签名剪枝
    "subtask_dispatch_totals": "monotonic",  # A2：终身账本【豁免 D08 剪枝】（剪了=熔断被绕，就是它要治的病）
    "subtask_alternate_ever_used": "monotonic",  # #33-CRITICAL：曾换备选持久账本（只增，replan 签名剪枝，同 subtask_dispatch_totals）——闸1 据它判"从未换过"防无界重触发
    "contract_retry_counts": "monotonic",  # D08 签名剪枝（D13 独立契约表）
    "subtask_redecompose_count": "monotonic",  # D08 签名剪枝
    "subtask_transient_counts": "monotonic",   # D08 签名剪枝（3.8 补）
    "exec_fail_sig_counts": "monotonic",   # #108：签名keyed 累计账（键是归一失败签名非 id，D08 id 剪枝天然不匹配→持久累积，正是熔断所需）
    "targeted_recovery_count": "monotonic",
    "targeted_recovery_counts": "monotonic",   # D08 签名剪枝
    "redispatch_wait_windows": "monotonic",    # R65TR-T3：D08 签名剪枝（观测账；id 复用继承陈旧账龄=账不可信）
    "abandoned_subtask_ids": "monotonic",      # D08 签名剪枝
    "give_up_isolated_ids": "monotonic",       # D08 签名剪枝
    "confirm_reason": "oneshot",        # REVISE 开新轮由 revision 清（3.8 修）
    "dispatch_remaining": "round",
    # 合并/验证
    "merge_conflicts": "round",
    "rebase_subtask_ids": "round",
    "subtask_rebase_counts": "monotonic",
    "merge_rebase_dropped": "monotonic",
    "l2_targeted": "oneshot",
    "l2_missing_fp_history": "round",   # R46-3：每次契约失败整体替换（连击追加/指纹变化重置）
    "verification_failure": "oneshot",
    "runtime_smoke_sandbox_id": "oneshot",
    "runtime_smoke_last_signature": "oneshot",  # 冒烟通过断链清（3.8 修）
    "adversarial_verify_round": "round",        # 收敛归零（3.8 修）
    "adversarial_verified_ids": "monotonic",    # token=sid@diff_sig 内容绑定自失效
    # 终态归因
    "deliver_auto_reject_reason": "oneshot",    # REVISE 开新轮由 revision 清（3.8 修）
    "base_commit": "terminal",
}


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
