# C1 设计文档：Anthropic 工程实践对照 → Swarm 优化方向

状态：**待拍板** · 版本 v0.1 · 2026-06-18
关联：ROADMAP P1 可维护性 · A1 状态外置 · B2 服务边界 · 8 篇 Anthropic 工程博客

---

## 0. 背景

调研 8 篇 Anthropic 工程博客（managed-agents / harness-design / infrastructure-noise /
effective-harnesses / code-execution-with-mcp / agent-skills / context-engineering /
writing-tools）。结论先行：**Swarm 的架构方向与 Anthropic 2025-2026 的 agent 工程高度一致**
（Brain/Worker 分离=decoupling brain from hands；A1 状态外置=session 外置、harness 是 cattle；
L1 确定性闸门=deterministic code over token generation）。这不是过时，是 frontier 同路。

本文把 8 篇的可落地启发映射成 Swarm 的具体优化项，分级 + 量化 + 风险 + 拍板疑问。

---

## 1. 核心启发 → 优化项总表

| # | 来源文章 | 启发 | Swarm 现状 | 优化项 | 级别 |
|---|---------|------|-----------|--------|------|
| I1 | managed-agents | harness 编码"模型现在不能做什么"，模型变强后变死重 | 大量节点假设"弱模型需硬约束兜底"，无能力感知 | 模型能力感知的约束降级 | P1 |
| I2 | infrastructure-noise | 沙箱 floor=ceiling 无 headroom → 瞬时峰值 OOM 杀死本可成功的任务（3x headroom 错误率 5.8%→2.1%） | CubeSandbox 固定 2c2g（floor=ceiling） | 沙箱 headroom 实验 + 资源分级 | **P0** |
| I3 | effective-harnesses | 防 premature victory：子任务清单是不可被 LLM 改写的事实表（JSON，只能改 passes） | 子任务完成靠 L1 闸门（已不信 LLM 自报 👍），但无结构化"完成事实表" | 子任务完成态事实表固化 | P1 |
| I4 | code-execution-with-mcp | 写代码批量调工具 + 中间结果在执行环境过滤，节省 98.7% token | Worker ReAct 逐步 tool call，L1 结果逐文件流过 context | L1 结果沙箱内聚合回传 | P1 |
| I5 | context-engineering | just-in-time 检索优于预灌；context rot（token 越多召回越差） | analyze 阶段预检索一次性灌入 | Worker 按需检索工具（范式级） | P2 |
| I6 | context-engineering | plan 常把独立子任务串行化 | dispatch 按 DAG 批次但 LLM 常加无谓 depends_on | plan 独立性后处理 + 自动并行 | P1 |
| I7 | writing-tools | 工具集臃肿/职责重叠 → agent 选不对；"人都说不清用哪个，AI 更不行" | Worker 工具集需审计（locate/code/verify 多 prompt） | Worker 工具集审计去重 | P2 |
| I8 | managed-agents | 凭证永不可达沙箱（git token 注入 remote / MCP 经 proxy） | CubeSandbox 经 dev_sidecar 代理（已隔离 👍），但需复核 token 是否真不可达 | 凭证可达性安全复核 | P1 |
| I9 | agent-skills | 渐进式披露：能力按需加载，不预灌 system prompt | Brain 各节点 prompt 固定全量 | 节点 prompt 渐进披露（低优） | P2 |

---

## 2. 分级与排序（性价比优先）

### P0 — 立即做，数据驱动，近乎零代码

**I2 沙箱 headroom 实验** 🔬
- **假设**：Java/Maven/Go 等重工具链子任务在 2g 硬限下，**装依赖阶段就 OOM**，根本到不了写代码——这可能是历史"fresh sandbox Java build 失败"的真根因（非模型问题）。
- **实验**：同一 Java 子任务，2c2g vs 2c4g vs 4c8g 三组各跑 N 次，对比 L1 编译/构建成功率 + OOM 率。
- **量化目标**：若 4g 组成功率显著高于 2g（参考 Anthropic 3x headroom：错误率 5.8%→2.1%, p<0.001），则证实根因，落地"按子任务语言/技术栈分级沙箱资源"。
- **成本**：改沙箱 create 的资源参数（CubeSandbox template 支持），跑对比。**风险极低**（不改业务逻辑）。
- **产出**：实验报告 + 若证实则加 `sandbox_resource_tier`（按 harness 语言路由 2g/4g/8g）。

### P1 — 高收益，局部改动，需测试护航

**I1 模型能力感知的约束降级**
- 强模型（Claude-4.6/GLM-5.1 级）路由时，降级部分重校验：review_design 高置信直接 approve、clarify 轮次上限调低、validate_plan 的 LLM 补充校验可跳过。
- **接上轮 O1/O2 诊断**，Anthropic 给了"约束变死重"的理论背书。
- 实现：`ModelCapabilityTier`（strong/standard/weak）→ 节点读 tier 决定约束强度。配置化，WebUI 可调。
- **风险**：降级过度会放回 bug。需 A/B：强模型降级组 vs 全约束组，对比 L1/L2 通过率与端到端延迟。

**I6 plan 独立性后处理 + 自动并行**
- LLM plan 常给独立子任务加无谓 `depends_on` 导致串行。加 heuristic 后处理：检测真实文件/契约依赖，剥离假依赖，让 dispatch 批次更宽。
- **量化**：端到端时延（独立子任务并行后批次数下降）。
- 实现：plan/elaborate 后加 `_decouple_independent_subtasks(plan)`，基于 scope 文件重叠 + shared_contract 判定。
- **风险**：误判独立 → 并行写冲突。merge 的冲突检测是兜底，但需保守（仅剥离"零文件重叠且无契约引用"的依赖）。

**I3 子任务完成态事实表固化**
- 借鉴 feature_list.json "只能改 passes 字段"思路：大任务的子任务完成态存为**不可被节点 LLM 随意改写的 PG 事实表**，防 replan/revise 多轮后的 premature victory。
- 现状：完成态散在 state + L1 结果。固化为单一事实表 + 完成判定只由确定性闸门写。
- **风险**：低（强化现有"不信 LLM 自报"原则）。

**I4 L1 结果沙箱内聚合回传**
- Worker 大变更集时，每文件 compile/lint 结果流过 context（context rot + token 成本）。改为沙箱内聚合，只回传 pass/fail + 失败摘要。
- 现状 `compress_tool_output` 已部分做，可更激进。
- **量化**：单 Worker 任务 token 消耗（大变更集场景）。

**I8 凭证可达性安全复核**
- Anthropic 强调"token 永不可达沙箱"。Swarm 经 dev_sidecar 代理已隔离，但需复核：沙箱内进程能否读到任何 LLM key / git token / DB 凭证？
- **动作**：审计沙箱环境变量 + 注入路径，确认零凭证可达。**纯审计，无代码**（除非发现泄漏）。

### P2 — 范式级 / 低优，需独立 DESIGN DOC

**I5 just-in-time 检索**（Worker 自主按需检索 vs analyze 预灌）— 架构级，影响 retrieval_top_k 语义，单独议。
**I7 Worker 工具集审计去重** — 中优，配合 writing-tools 的 eval 驱动法。
**I9 节点 prompt 渐进披露** — 低优，收益有限。

---

## 3. 待确认疑问（拍板表）

| # | 疑问 | 选项 | 倾向 |
|---|------|------|------|
| Q1 | 首批做哪些？ | a.仅 P0(I2实验) / b.P0+P1精选(I2+I1+I6) / c.全 P1 | **b**：I2 实验最快见效，I1/I6 收益最大 |
| Q2 | I2 实验沙箱资源谁出？ | CubeSandbox template 改资源 / 临时手动调 | 先临时调跑对比，证实再固化 tier |
| Q3 | I1 降级是否默认开？ | 默认开(激进) / 默认关需显式启用(保守) | **保守**：默认关，WebUI 可启用 + A/B 验证后再默认 |
| Q4 | I6 并行的安全边界？ | 仅剥离零文件重叠 / 更激进 | **仅零重叠+无契约引用**，merge 冲突检测兜底 |
| Q5 | P2 范式级是否现在排期？ | 现在排 / 等 P0/P1 落地后再议 | **等**，先把数据驱动的 P0/P1 做实 |

---

## 4. 不做什么（避免过度工程）

- ❌ 不引入 MCP code-execution 全套（I4 取其思想即可，Swarm 非 MCP 架构）
- ❌ 不为 I9 重构所有节点 prompt（收益 < 风险）
- ❌ 不推倒 analyze 预检索改全 just-in-time（I5 需实测验证再定，非拍脑袋切换）

---

## 5. 与既有 ROADMAP 的关系

- I1/I3 强化 A1/A2 已有的"状态外置 + 不信 LLM 自报"原则。
- I2/I4 优化 Worker 执行层，与 B2 服务边界正交（可独立做）。
- I6 优化 dispatch 并行度，是 B2 之外的 throughput 提升。
- 整体不与 B2/Docker 冲突，是 P1 可维护性/性能的并行增量。
