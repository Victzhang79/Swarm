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


def _merge_verification_coverage(
    old: dict[str, str] | None, new: dict[str, str] | None
) -> dict[str, str]:
    """LangGraph reducer for ``verification_coverage``（B-7/V-C3）—— 浅合并按格覆写。

    为什么是 reducer 而非 last-write-wins：各验证节点（verify_l2 / verify_runtime /
    verify_l3）【各写各的格】，last-write-wins 会让后写者把先写者的格整表抹掉。
    同格覆写=重入/重试取最新轮次（与 runtime_smoke 键族"整体替换防粘滞"同哲学，
    粒度从整表细到格）。任一侧 None 容错为 {}。
    """
    out = dict(old or {})
    for k, v in (new or {}).items():
        out[str(k)] = str(v)
    return out


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
    # ★32 号文 A5-L1★ `user_profile: dict`（L1 画像原始 JSON）已删——写在 runner.py 初始
    # state 里但**全仓零读点**：两个派生消费点 `shared._brain_profile_prompt` /
    # `_worker_profile_prompt` 读的都是下面两个 prompt 键。★易误判点：`user_profile` 同时是
    # prompt **模板占位符名**（`brain/prompts.py:29,202,293` / `worker/prompts.py:102`），
    # 裸 grep 会命中一堆 `user_profile=` 而那些传的是派生 prompt，不是本键★。
    # 要在节点里读结构化画像：调 `memory/profile.py:resolve_user_profile`（现取，不是任务
    # 起点的陈旧快照）；要注入 LLM：用下面两个 prompt 键。
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
    coverage_design_attempted_reqs: list[str]  # ★L2 补排闸的"试过了"账（26 号文 C-7 治本）★ 补排为哪些 req 试过设计文件、试完仍未覆盖。补排闸据此不再对同一条重试——round67m2 实证：伞形需求 req-27e9b283（判别 token 退化到只剩 ['prd']，结构上永不可能被 vocab 匹配满足）每轮都判 unplanned → 每轮触发补排 → LLM 在【不含既有 file_plan】的 prompt 里凭空造 48-69 个文件的平行设计（三轮三套互不相同命名，Alarm→Alert 都换了），file_plan 234→303→350 单调膨胀 → 大量 create 撞 base → ③f 准确 REJECT → 熔断。与 B1 修复记忆同族：没有"试过了"的记忆，就会无界重入
    merge_owner_drops: list           # ★C-4（26 号文）★ owner 裁决丢件机读账：[{file, owner, dropped:[sid], dropped_lines}]。merge 的 owner 通道整份丢弃非 owner 写者版本且刻意不进 rebase（理由正当：确定性修复"碰过"的文件重做多少次还会被碰到）；但此前全仓只有一行 WARNING、零机读账，而 owner 判据来自 plan 声明的写权——plan 声明错时被丢的可能正是真产出，交付面完全无感。每轮 merge 无条件重写（clean 路径写 []）
    merge_owner_unions: list          # ★#29-8 H-1★ owner 裁决【并集成功】机读账：[{file, owner, unioned:[sid]}]——非 owner 内容已并入交付物，一行没丢，与 drops 分账（混记=假账冤杀 L6/人工闸/M-6 闸）。每轮 merge 无条件重写（clean 路径写 []）
    plan_validation_issues: list[str]   # PlanValidator 问题列表
    plan_validation_gate: str          # ★本轮 validate 死在【哪道闸】（复核 H-3）★ validate_plan 是 9 道顺序早退闸，plan_validation_issues 只含【第一个失败闸】的 issues。反回归段若不知产出闸，就会把"本轮压根没跑过的闸"的历轮 issue 当成"已修掉、绝不许回归"（跨闸弹跳假阳性，与被治的 renumber 假阳性同危害不同根）。取值见 nodes/__init__.py:_VALIDATE_GATE_ORDER
    plan_validation_warnings: list[str]  # G3-2：规划期软警告（规则5 落空/C1 无主符号）机读面——★R67M2-T3：validate_plan 全部 11 个 return【恒发】（含各早退与成功轮），空列表=本轮无软警告的如实表达；此前"非空才带"的条件发射与本键 round 注册相矛盾（LangGraph 对缺席键保持原值→上轮 warnings 粘滞进 payload 白名单/API/复盘面，B3 的 T4 文案自带本轮计数语义，粘滞即假信号）★
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
    # H-6（SPEC_h6_file_plan_reconciliation）：file_plan 裁决账——append-only，每条
    # {round, pass, action(strip|relocate|dedupe), path, owner_path}。写入方=#101/层③/R67G
    # file_plan 预消解/CVB 归位（经 resolve_plan_conflicts + PLAN 节点 R67G 调用）；
    # 消费方=PLAN 重拆前 reconcile_file_plan_ledger（裁决重放+膨胀收缩）与孤儿挂靠前置核
    # （attach_orphan_file_plan_entries）。已裁决的违例绝不随全量重拆/挂靠/L2 补排复活
    # （元模式1/2 根修）。REVISE/replan 新周期清空（同 prev_structural 纪律）；retry 轮不清
    # （跨 retry 轮正是其存在意义）。
    file_plan_adjudications: list[dict]
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
    # F3：L3 跳过原因机读键（always-emit：6 条跳过 return 各带具体值、通过/失败路径带 ""）。
    # 治前 6 处跳过逐字同构、机读不可辨是【哪种】跳过；其中 push_failed/llm_unavailable 两分支
    # 不写 degraded ⇒ L6 should_write_success 把"L3 没验"学成成功（毒化断链，比登记更重）。
    l3_skip_reason: str                 # F3：L3 跳过原因（""=未跳过）
    verification_failure: str | None    # l2 / l3 / runtime_smoke 等验证失败来源（handle_failure 专类分支据此归因）
    # ★B-7/V-C3（27 号文 §6.3 原则 3）★ 验证覆盖账：每道确定性验证闸写一格
    # （l2 / runtime_smoke / l3），值域 passed | passed:unverified | failed | skipped |
    # unsupported_stack:<栈键>（★R2 hunter M-1★ passed:unverified = l2_passed=True 但
    # 无测试命令/测试降级 LLM/编译未核验——"放行"与"验过"必须机读可分，否则消费者
    # 谎称"验过"）。治前"四闸全 None（都没验）仍 auto_accept 放行"只靠人读
    # degraded_reasons 散文可辨；本账让"验了没有/验了几道/哪道没实现"机读可查。
    # 声明先行铁律同下：不声明则被 LangGraph 静默丢弃。reducer=浅合并按格覆写
    # （各闸各写各格，重入取最新轮——★R2 hunter H-1★ 这正是它相对 append-only 的
    # degraded_reasons 的价值：本账是【本轮事实】，gates 的未支持栈判据在场即以本账
    # 为准，旧轮粘滞条目不再误拦）。
    # 消费者：deliver payload 明示（runner._build_result_payload）+ gates 拒因文案
    # 与未支持栈判据（can_auto_accept_delivery）。本键的 passed:unverified 格是
    # 【观测账】不是新硬闸——那三族本来就不硬拦 auto_accept（拦 L6 假学习走
    # should_write_success 的 degraded 通道）。
    verification_coverage: Annotated[dict[str, str], _merge_verification_coverage]

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
    contract_symbol_paths_unhealed: list  # R67E-P2（round-2 hunter Finding B）：本轮 finish 后仍未愈的契约类名 file-path 分叉符号名 [X,...]（greenfield 已愈=[]，棕地/歧义 punt 或畸形残留=非空，将死 L2）——★last-write-wins 观测键，绝不进 append-only degraded_reasons（coverage_gap_residual:196 同律：那里无人能清，愈合后陈旧粘滞会永久误拦 should_write_success 学习+误导 deliver）★；plan 节点 always-emit（愈合清空不粘滞）；纯诚实观测非门（未愈将由 L2 真失败兜底门，刻意不硬 REJECT 避免复刻 round67e 名分叉重产不收敛熔断）
    dep_ban_reconciled: dict            # R67M-T1（复核 A6/hunter F4）：本轮 finish 依赖禁令散文自愈成功账 {sid: {old, coords}}——always-emit last-write-wins 观测键（无命中={} 清空不粘滞），成功账零消费=新账无人收盲区；失败侧 dep_ban_reconcile_failed 走 *_failed 通用扫尾进 degraded（崩溃≠零命中）
    dep_versions_unverified: dict        # P-C2 复核 F-2：本轮 P-C2 依赖版本闸【未能证实】的坐标 {module: ["pkg@spec(unverified|unjudgeable)", ...]}——三种结局（确证存在/不可达 fail-open/刻意不判）原先全塌成 source="explicit"，而国内环境 proxy.golang.org 常不可达时 F1 收紧后 proxy_version_exists 永不返 False ⇒ 闸整轮静默失效且交付物与闸正常时逐字相同（唯一信号是 WARNING，而纪律 #106 禁止解析 swarm.log）。always-emit last-write-wins（无命中={} 不粘滞，绝不进 append-only degraded_reasons）；★刻意非门★——不可达是环境常态，拿它拦 auto_accept 会让每个 plan 都 degraded ⇒ 使用者必然绕开
    exam_rule5_dropped: dict            # ★31 号文 A2-H1★：本轮 finish 里被考卷同源对账【删除且无等价回填】的规则5 依赖要求 {sid: [原验收行, ...]}——病灶=owner 上多条规则5 机器行（normalize 的 _sole_owner N:1 归并产物）被【只含单模块 artifacts 的权威模板】坍缩成一条，其余契约模块的真实依赖要求静默消失（实测 3 条→1 条，freemarker/hutool-all/okhttp 全丢），而治前唯一痕迹是 acceptance_rewritten 计数、日志不列内容 ⇒ "少了哪条依赖要求"只能靠考古。按仓内纪律【静默丢需求比矛盾考卷更坏】。根因已在 _inject_templates_into_pom_owners 取 artifacts 并集处治掉，本账是第二道观测网（并集取证 fail-open / _single_tpl 兜底路径仍可能走到）。always-emit last-write-wins（无命中={} 不粘滞，绝不进 append-only degraded_reasons）；★刻意非门★——拿它拦 auto_accept 会在取证 fail-open 的环境里让每个 plan 都 degraded ⇒ 使用者必然绕开
    contract_symbols_base_referenced: list[str]  # R67M-T2 B5（23号文，round67m CVB 死因治本）：本轮 finish 安置前 base 查表转换账 ["符号→base路径(案由)"]——被认出为存量引用而跳过影子安置的契约符号（防 G1 ③f _created_class_shadows_base 硬打回）。always-emit last-write-wins 观测键（无转换=[] 不粘滞），成功账零消费=新账无人收盲区
    t4_ambiguous_types: list[str]   # R67M2-T3 B3（24号文，round67m2 已见未治治本）：elaborate T4 布线检出的多落点歧义类型账——round67m2 实证"跳过布线"WARNING 轮2/3 各一次却零账可查（已见未治）。always-emit last-write-wins 观测键（无歧义=[] 不粘滞）；ambiguity 本体交 ③b fail-closed，此账只解决复盘盲区（24号文拍板先观测不单开闸）
    symbol_exam_dropped: dict          # ★31 号文 A1-M2★：本轮 B3④（H3b↔考卷同源对账）因成环剔除的验收正断言 {sid: [被剔断言, …]}——剔除方向本身刻意且正确（该断言此刻确定性不可满足，与"不得 import"提示打架=卷子必死），问题是治前**账只活在日志里**：不进返回值、不进 out、不进 state，而纪律 #106 明令进度/状态判读绝不解析 swarm.log ⇒ "这个子任务的验收面被确定性拿掉了"在机读面完全不可见（实测可剔到 0 条）。★绝不能用 symbol_cycle_pairs 反推★——环对存在≠有断言被剔（消费者可能本来就没正断言），不同事实不得共用一个账。消费者：validate_plan 折进 plan_validation_warnings（API/盯跑/deliver 已有读者）+ get_task_progress。always-emit last-write-wins（无命中={} 不粘滞）
    symbol_exam_zeroed: list           # ★31 号文 A1-M2③★：B3④ 剔除后验收面【归零】的子任务 id——与"剔了一部分"必须可区分（不同后果必须分账，否则响铃永远响在错的位置）：零验收=该子任务从此无任何专项确定性验收，只剩 L1 编译/测试面。B3⑤ 裸奔闸跑在 B3④ **之前**且只管 create-pom 子任务，普通代码子任务被剔到零后没有任何 pass 会回头补。消费者同上（validate 侧独立文案 + progress）。always-emit last-write-wins
    contract_symbols_layout_punted: list[str]  # R67M2-T2 C1（24号文，复核 HIGH-2）：本轮 finish 安置落点布局闸 punt 账 ["符号→落点路径"]——落点不在 JVM 可编译源码布局内（幽灵路径=mvn 不编译假过）而【确定性永不建安置】的契约符号。validate_plan C1 owner 闸消费：punt 符号仍无主时【不占 0.4 无主宽容直接硬打回】（防胖契约下符号静默蒸发）。always-emit last-write-wins（无 punt=[] 不粘滞）
    plan_validation_issue_history: list[str]  # R67M-T2 B1（23号文，round67m 主死因治本）：VALIDATE→PLAN 重试循环的【修复记忆】——历轮校验 issues 去重累积（increment_retry 单点，全闸种必经）。round67m 实证：只注上轮 issues+全量重拆=非单调振荡（轮4 CVB shadow=轮1 逐字回归烧 3h15m）；PLAN 注入点把"历轮曾现而本轮已消失"的缺陷作"绝不许回归"硬约束注入。清空纪律：validate 通过 / REVISE·failure replan 新周期（与 plan_validation_prev_structural 同点对称）
    clarify_blocked_by_facts: bool      # 虚假前提阻断：auto 模式也不能用默认假设硬跑，需人工澄清/终止
    design_review: dict                 # {decision: approve|reject, feedback, reject_count}
    # ─── 渐进明细(两层)───
    # ★32 号文 A5-L1★ `plan_elaborated: bool` 已删——纯死标志位：两个写点
    # （`planning_nodes.py` elaborate 出口）都无条件写 True，而**路由不读它**
    # （`brain/graph.py` 零引用），全仓零消费者。它想表达的事实由 `plan.subtasks` 非空
    # 直接派生，无需第二份账（第二份账 = 会漂移的账）。
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
    "coverage_design_attempted_reqs": "monotonic",  # C-7：重试窗口内单调累积（补排闸单点写）；与 plan_validation_issue_history 同清空点（validate 通过 / REVISE·replan 新周期）
    "merge_owner_drops": "round",      # C-4：每轮 merge 重算覆盖（last-write-wins）；条件 emit 会让上轮丢件账粘滞成"本轮也丢了"
    "merge_owner_unions": "round",     # #29-8 H-1：与 drops 严格同生命周期（同一次 merge 一起写、一起被下轮覆盖）
    "plan_validation_issues": "round",
    "plan_validation_gate": "round",   # 与 plan_validation_issues 严格同生命周期（同一次早退一起写、一起被下轮覆盖）
    "plan_validation_warnings": "round",  # G3-2：last-write-wins（每轮重算）
    "plan_validation_feedback": "oneshot",
    "plan_batch_cache": "round",
    "plan_batch_failed_modules": "round",
    "contract_symbol_paths_unhealed": "round",  # R67E-P2：last-write-wins 观测键，愈合清空不粘滞（Finding B）
    "dep_ban_reconciled": "round",  # R67M-T1（复核 A6）：自愈成功账 last-write-wins 观测键，无命中={} 不粘滞
    "dep_versions_unverified": "round",  # P-C2 复核 F-2：闸未能证实的坐标账 last-write-wins，无命中={} 不粘滞
    "exam_rule5_dropped": "round",  # 31 号文 A2-H1：考卷对账删掉的规则5 依赖要求账 last-write-wins，无命中={} 不粘滞
    "contract_symbols_base_referenced": "round",  # R67M-T2 B5：base 查表转换账 last-write-wins 观测键，无转换=[] 不粘滞
    "t4_ambiguous_types": "round",  # R67M2-T3 B3：T4 多落点观测账 last-write-wins，无歧义=[] 不粘滞
    "symbol_exam_dropped": "round",  # 31 号文 A1-M2：B3④ 剔除的验收正断言账 last-write-wins，无命中={} 不粘滞
    "symbol_exam_zeroed": "round",  # 31 号文 A1-M2③：验收归零子任务账 last-write-wins，无命中=[] 不粘滞
    "contract_symbols_layout_punted": "round",  # R67M2-T2 C1：布局闸 punt 账 last-write-wins，无 punt=[] 不粘滞（C1 owner 闸消费=硬打回面）
    "plan_validation_issue_history": "monotonic",  # R67M-T2 B1：重试窗口内单调累积（increment_retry 单点）；validate 通过/REVISE·replan 新周期整体清空（与 prev_structural 同点）。复核 LOW-4 口径注：prev_structural 注册 "round" 因其值每轮【重算覆盖】（last-write-wins），本键值在窗口内【只增不改】——累积语义差异故分类不同，非同律漂移
    "baseline_covered": "round",
    "coverage_watermark": "monotonic",
    "baseline_ineligible_reqs": "monotonic",  # R65E9-T1：拒掉的假 baseline_covered id 单调累积
    "coverage_gap_residual": "round",   # A6 残差 last-write-wins：gap 放行覆写/全覆盖清空（3.9 H-F5）
    "plan_soft_review_sig": "round",    # 只在真放行时 emit，否决轮发空串（3.9 H-F6/R-F5）
    "plan_validation_prev_structural": "round",  # R64-T3：G1 失败轮整体替换；retry 绑定免疫陈旧残留
    # H-6：file_plan 裁决账。周期内 append-only（跨 validate retry 轮正是其存在意义——重拆
    # 不复活已裁决违例）；REVISE/replan 新周期【整体清空】后由确定性 pass 重推导（同
    # prev_structural 纪律；清空安全=同一条 file_plan 同一批确定性 pass 必重判同违例）。
    "file_plan_adjudications": "monotonic",
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
    "l3_skip_reason": "round",  # F3：verify_l3 每次执行 always-emit 覆写（通过/失败=""），无残留语义
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
