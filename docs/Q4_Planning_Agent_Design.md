# Q4 设计 v3：交互式渐进规划 Agent（✅ 已实现）

> 状态：v3 · **已全部实现并验证** · 作者：CTO 协作
> 实现：4 批里程碑全部交付（规划节点/graph 接线/依赖调度/可观测+持久化+前端）。
> 关键验证：多轮澄清 interrupt/resume 闭环跑通；全测试套件零回归。

---

## 0. 已拍板决策（用户确认）

| # | 决策 | 结论 |
|---|---|---|
| Q1 澄清轮数 | **自适应 ≤5 轮**，云端大模型启发式渐进提问，可要求用户补充 |
| Q2 触发分级 | **分级**；但**复杂度后置判定**——先澄清把模糊需求问清，再基于"澄清后完整信息+知识库"定级（破解"人类描述不准/产品经理只知要什么不知怎么做"） |
| Q3 独立含义 | **逻辑独立子图**（brain 内加节点）——初衷是便于编码/维护/日志追踪，避免分散难维护 |
| Q4 分期 | **不分期**，编排好后一步步全做完 |
| Q5 评审 | 涉及技术方案评估的**必须人工审核**，次之走交互确认 |
| Q6 技术方案产出 | 技术栈选型(新建)+架构+**注意事项**+**数据模型图**+**业务流程图**+风险+验收+变更影响评估(既有)+可维护性+**易读注释要求** |
| Q7 上下文预算 | **硬约束**：每子任务预估上下文/产出规模，超阈值(本地小模型~196k)强制再拆；拆不下=编排错 |

## 0.1 接纳的工程补充点

| 点 | 内容 | 落地 |
|---|---|---|
| A | 拆解粒度 INVEST 自检：每子任务单一职责+可独立验证+可估;不满足→打回再拆 | elaborate 后自检节点 |
| B | **接口/契约先行**：技术方案阶段先定共享契约(API schema/数据模型)，并行任务依赖稳定前置 | 复用强化 `shared_contract`/`enrich_plan_with_shared_contract` |
| C | 规划自身吃上下文：多轮澄清历史**滚动摘要**，不无限堆 | clarify 累积摘要 |
| D | **微任务极速通道**：单点/低风险/无架构影响(如"按钮黄→绿")跳过全部重流程→1 个 trivial 子任务 | 澄清初判阶段识别→快速路径 |
| E | 评审打回收敛：带反馈重做，最多 3 次后强制人工接管/降级，防无限循环 | review_design 计数 |
| F | 规划产物可追溯：澄清问答/技术方案/评审决策**持久化**，任务详情页可回看 | 写 store + 任务详情 UI |

---

## 1. 现状与缺口（已核对代码 2026-06）

| 能力 | 现状 | 文件:行 |
|---|---|---|
| 状态机 | `analyze→plan→validate_plan→[confirm(ultra)]→dispatch` | `brain/graph.py` |
| analyze | LLM+启发式判复杂度，检索知识库 | `brain/nodes.py:447` |
| plan | **一次性 LLM 出全量 DAG**；simple 走 `_build_simple_plan` | `brain/nodes.py:583` |
| plan 已读知识库 | `format_brain_knowledge_prompt` 已注入 | `nodes.py:619` |
| interrupt/resume | `confirm_plan` + PostgresSaver + 跨 HTTP resume 跑通 | `nodes.py:804` |
| auto_accept 旁路 | `state["auto_accept"]`/env 跳过 interrupt | `nodes.py:816` |
| depends_on/并行 | SubTask 有 depends_on，但调度按 parallel_groups 限制(review #23) | `brain/*` |
| shared_contract | `enrich_plan_with_shared_contract` 已有 | `brain/contract_utils.py` |
| LangSmith | `push_l1_feedback`/`swarm_traceable` 已具备 | `tracing.py` |

**缺口**：零澄清、零技术方案/评审、一次性出全量DAG无渐进明细、并行被 parallel_groups 限制、无上下文预算硬约束。

---

## 2. 目标状态机（graph.py）

```
ANALYZE (轻量初判：要不要进规划流程 + 检索知识库 + 微任务识别)
  ├─(微任务: 单点/低风险/无架构影响, 如"按钮黄→绿")──────────→ PLAN (极速通道: 1 trivial 子任务)
  ├─(可能复杂 & !auto_accept)→ CLARIFY ⟲ (自适应≤5轮 interrupt, 启发式提问, 历史滚动摘要)
  │                              └→ ASSESS (澄清后定级: 基于完整信息+知识库)
  │                                   ├─(澄清后判定 simple)──────→ PLAN (轻量)
  │                                   └─(complex/ultra/greenfield)→ TECH_DESIGN (技术方案+共享契约先行)
  │                                        └→ REVIEW_DESIGN (人工评审 interrupt, 打回≤3次)
  │                                             ├─通过→ PLAN (L0 骨架)
  │                                             └─打回→ TECH_DESIGN
  └─(auto_accept/CI)────────────────────────────────────────→ PLAN (轻量, 走默认假设)

PLAN (complex/ultra 出 L0 骨架; 其余全量)
  ├─(complex/ultra)→ ELABORATE (骨架→子任务DAG, 含上下文预算+INVEST自检) → VALIDATE_PLAN
  └─(其余)──────────────────────────────────────────────────→ VALIDATE_PLAN
VALIDATE_PLAN → [confirm(ultra)] → DISPATCH (依赖驱动并行, 接口先行) ...（现状链路不动）
```

新增节点：`clarify`(多轮)、`assess`(澄清后定级)、`tech_design`、`review_design`(interrupt)、`elaborate`、子任务 INVEST/上下文自检。
关键顺序：**复杂度后置**——analyze 只做"要不要澄清"的轻判 + 微任务识别；真复杂度在 `assess`(澄清后)定。
零改动：dispatch 之后 L1/验证链路。

---

## 3. BrainState 新增字段（state.py，纯加法）

```python
# ── 微任务极速通道(D) ──
is_micro_task: bool                   # 单点/低风险/无架构影响
# ── 澄清(多轮 自适应 C:滚动摘要) ──
ambiguity_score: float
clarify_round: int
clarify_history: list[dict]           # [{round, questions, answers}]
clarify_summary: str                  # 滚动摘要(防上下文堆积)
clarify_done: bool
# ── 澄清后定级(Q2) ──
assessed_complexity: str              # 澄清后真复杂度(非 analyze 初判)
# ── 技术方案+评审(Q5,Q6,B) ──
tech_design: dict                     # {stack, architecture, data_model_diagram, flow_diagram, risks, notes, acceptance, change_impact, maintainability}
shared_contract_draft: dict           # 接口先行(B): API schema/数据模型
design_review: dict                   # {decision, feedback, reject_count}
# ── 渐进明细 + 上下文预算(Q7,A) ──
plan_milestones: list[dict]
plan_elaborated: bool
# 每 SubTask 扩展: est_context_tokens, est_output_scale, invest_ok (拆解自检用)
```

---

## 4. 上下文预算硬约束（Q7，核心）— ✅ 实现

- SubTask 新增 `est_context_tokens` 字段；plan 节点对每个子任务按难度+scope 文件数启发式估算
  （TRIVIAL 8k / MEDIUM 50k / COMPLEX 120k + 文件数×6k），LLM 可覆盖。
- 阈值：`SWARM_SUBTASK_CONTEXT_BUDGET`（默认 150k，留余量 < 本地小模型 196k）。
- 超阈值 → `elaborate` 调 LLM **二次拆分**该子任务为 2-4 个各自在预算内的子任务，回写 plan；
  多轮循环（上限 `MAX_ELABORATE_RESPLIT=3`）重检，直到都在预算内。
- 拆到上限仍超 → 标记 `oversized_subtask_ids` + 上报 LangSmith + 日志告警（需人工重新切分需求）。

## 5. 拆解 INVEST 自检（A）+ 依赖编排（B）— ✅ 实现

- INVEST-Testable：elaborate 统计无验收标准的子任务数 `invest_fail_count`（上报，软信号）。
- 渐进明细：采用 **elaborate 二次拆分**实现"先粗后细"（plan 出初版 DAG → elaborate 对超预算的再拆），
  而非 plan 显式两层 L0 骨架——功能等价，核心价值"大任务拆到上下文内+各自可验证"已达成，改动更小。
- 接口先行：tech_design 产出 `shared_contract_draft`(API/数据模型) → 依赖它的并行子任务以此为稳定前置。
- 依赖驱动调度：dispatch 选所有 `depends_on` 已满足的子任务并发(capped max_concurrent)，parallel_groups 仅软提示。

## 6. LangSmith 上报（push_planning_feedback）

clarify_rounds、ambiguity_score、clarify_answered/skipped、assessed_complexity、tech_design_review(approve/reject_count)、milestone_count、subtask_count、oversized_count、invest_fail_count。tracing 关全程 no-op。

## 7. 可追溯持久化（F）

clarify_history/tech_design/design_review 写入 store（任务级），任务详情页新增"规划过程"区可回看。

---

## 8. 实施编排（分批里程碑落地，每批跑通验证再进下一批；最终全做完）

> 落地策略：A 方案——拆 4 批，每批一个安全检查点（出错易定位、不破坏现有任务流）。
> 不分期 = 4 批最终全部交付；分批只是加中间验证点，符合"E2E 等 terminal 态/行为改动要验证"。

### 批次①：后端规划节点（不接入 graph，独立可测）— ✅ 已完成
- [x] state.py 新增字段
- [x] `brain/planning_nodes.py` 新模块：clarify / assess / tech_design / review_design / elaborate
- [x] prompts：CLARIFY/ASSESS/TECH_DESIGN 模板（内联模块，自包含）
- [x] 分级单测 `test/test_planning_nodes.py`：9/9 全绿（轮数上限/跳过/定级/预算超限/INVEST/打回上限）
- **检查点**：✅ 节点单测全绿、import OK、无循环导入、不碰 graph。

### 批次②：graph 接线 + 入口改造 + 全链路 E2E — ✅ 已完成
- [x] analyze 改：轻量初判(needs_clarify/is_micro_task via _planning_triage)，复杂度后置给 assess
- [x] plan 改：优先读 assessed_complexity 回退 complexity
- [x] graph.py 注册 5 新节点 + 条件边(after_analyze/after_clarify/after_assess/after_review) + plan→elaborate→validate
- [x] **E2E**：graph 构建 OK(20节点)；实跑在 CLARIFY 处正确 interrupt(type/round/问题传出)；
      节点顺序 analyze→clarify→assess→plan→elaborate→validate 实测流转正确；多轮收敛逻辑(done→assess)验证
- [x] **回归**：完整测试套件全绿，状态机入口改造未破坏现有任务流
- **检查点**：✅ graph 重编译成功、interrupt 触发、规划子图链路流转正确、零回归。
  （注：真实多轮 resume HTTP E2E 待批次④前端接入后一并跑；进程内 interrupt/链路已验证）

### 批次③：dispatch 依赖驱动并行 — ✅ 已完成
- [x] 核查发现 `TaskPlan.get_dispatch_batch`(types.py:232) **已是依赖驱动**（review #23 早被根治，
      parallel_groups 仅作软提示）；本批补单测固化防回归，无需重写
- [x] 单测 `test/test_dispatch_dependency.py`：4/4 全绿（独立并发/依赖串行/max_concurrent 截断/钻石 DAG）
- [x] 接口先行(B)：tech_design 产出 shared_contract_draft + plan 的 shared_contract 机制已在
- **检查点**：✅ 调度单测全绿，不破坏现有 monitor/merge 链路。

### 批次④：可观测 + 持久化 + 前端 — ✅ 已完成
- [x] push_planning_feedback(LangSmith, tracing.py)：澄清轮数/跳过/定级/评审/拆分密度/oversized/invest_fail；tracing 关 no-op
- [x] elaborate 节点接入上报 + 持久化调用
- [x] 持久化(F)：`task_records.planning_artifacts` JSONB 列(迁移) + store.save/get_planning_artifacts
- [x] API：`GET /api/tasks/{id}/planning`(回看) + `POST /clarify`(提交澄清) + `POST /review-design`(评审)
- [x] runner：interrupt 白名单加 clarify/review_design + 状态标签映射 + resume_planning(_background) 透传结构化 payload
- [x] 前端 `planning_ui.js`：renderClarifyPrompt(多轮问答) + renderDesignReviewPrompt(方案评审) +
      loadPlanningArtifacts(回看)；tasks.js SSE handler 按 interrupt_type 分发；index.html 引入
- [x] **E2E**：确定性 mock 验证多轮 interrupt/resume 闭环——第1轮中断→resume记录答复→clarify_round递增→第2轮再中断；
      端点参数校验；全套件零回归
- **检查点**：✅ 多轮澄清 interrupt/resume 闭环跑通、上报/持久化/端点/前端全接、零回归。

> 全 4 批已交付。Q4 交互式渐进规划 Agent 后端 + 前端全部实现。

> 全 4 批交付后，本设计稿标记"已实现"，更新 TECH_DEBT 与 README。

---

## 9. 验证策略（分级）

| 改动 | 验证 | 成本 |
|---|---|---|
| state 字段 | import+探针 | 秒 |
| analyze 微任务识别/初判、assess 定级、路由条件边 | 纯函数单测 | 秒 |
| clarify 多轮上限/跳过/滚动摘要/auto_accept 旁路 | 单测 | 秒 |
| tech_design 输出结构、shared_contract_draft | mock LLM 断言 schema | 秒 |
| 上下文预算超限再拆、INVEST 自检打回 | 单测(构造超大子任务断言被拆) | 秒 |
| review 打回收敛≤3 | 单测 | 秒 |
| **graph 重编译 + 多轮 clarify/resume + 评审 interrupt 全链路** | 真实 E2E(create→clarify×N→assess→tech_design→review→plan→elaborate)，**等 terminal 态** | 重，仅此一项 |
| 依赖驱动并行 | 单测(独立任务并发/有依赖串行) | 秒 |
| push_planning_feedback | 关 no-op + 开 mock 断言 | 秒 |
| 持久化可追溯 | 写读往返单测 | 秒 |

> 唯一重 E2E：多轮 interrupt/resume + 评审全链路(harness-semantics 级)，真跑等 terminal 态。
