# DESIGN DOC — 任务 0f93f1fc 暴露的编排链路缺陷修复

> 状态: **待确认**（编码前需你拍板，尤其末尾「待确认疑问」表）
> 范围: `brain/planning_nodes.py`、`brain/graph.py`、`brain/nodes/__init__.py`、`brain/nodes/dispatch.py`、`brain/plan_validator.py`、`tracing.py`
> 产品哲学锚点: **大模型带知识库+记忆做编排 → 渐进明细收集足够信息 → 交互式确认（出选项+输入框让人填）→ 编排成多任务/多容器，独立则并行**

---

## 0. 这次失败的一句话总结

一个本该 5 分钟搞定的 medium 任务（建 NumberUtils + 改 StringUtils），跑了 **21 分钟、零交付、FAILED**。
worker 其实把代码都写对了（L398/L492 自述 + diff=5694），是**编排层**把它拖死的：
**ELABORATE 拆分后没重写下游依赖 → 规划死循环 → auto_accept 放行非法计划 → scope 错配 → replan 重入 → LangGraph 递归上限 25 撞穿崩溃。**

---

## 1. 问题清单（每条钉在日志行 + 代码行）

| # | 严重度 | 现象（日志） | 根因（代码） |
|---|---|---|---|
| P0-1 | 🔴 致命 | L18-41 ELABORATE 把 st-1 拆成 st-1-1/st-1-2，但 st-2 仍 `depends_on=[st-1]`，结构校验连报 4 次"依赖未知任务 st-1" | `planning_nodes.py` L582-589 `_resplit_subtask` 原地替换节点，**未重写指向被拆 id 的下游 depends_on** |
| P0-2 | 🔴 致命 | L517 `Recursion limit of 25 reached` 框架崩溃 | `tracing.py` `brain_graph_config` 从不设 `recursion_limit`（默认 25）；且规划失败循环 + replan 重入无熔断 |
| P0-3 | 🔴 致命 | L42-45 校验失败 4 次 → CONFIRM → `auto_accept` 直接 ACCEPT，把**已知非法**计划送进 DISPATCH | `graph.py` L144-146 失败耗尽路由到 confirm；`nodes/__init__.py` L541-543 auto_accept 无视 `plan_valid=False` 一律放行 |
| P1-1 | 🟠 高 | L508 scope 配置冲突：NumberUtilsTest.java 创建权在 st-1-1，st-1-2 改它却无 writable → scope_violation → empty_diff | PLAN 切 scope 时同一文件 create/write 权限错配；被依赖产物未自动进下游 readable/writable |
| P1-2 | 🟠 高 | L266 "st-1-1 L1=通过" 与 L112/157/200/251 "未通过 ❌" 直接矛盾；L398 `compile_passed=True` 却 `reason=empty_diff`；L504 diff=5694 仍判 empty | L1 判定无单一事实源；dispatch 层"通过"与 executor 层"未通过"各写各的；模型拒答串 `Sorry, need more steps...` 被吞进 raw_result 当 deterministic |
| P2-1 | 🟡 中 | L398/L492 worker 正确诊断出 StringUtils 缺 Constants/StrFormatter/CharsetKit 编译失败，但信号被埋没 | PLAN 给 st-1/st-1-2 的 `readable` 范围不含同包依赖类，同模块编译注定失败 |
| P2-2 | 🟡 中 | L536 无条件打印"等待人工确认 (ultra 复杂度)"，实际是 medium + validation_failed | `nodes/__init__.py` L536 写死文案，没用 L548-554 已算好的 reason/_msg |
| P2-3 | 🟡 低 | L46 "派发 1 个子任务（剩余=3）"，3 来自 st-1-1+st-1-2+st-2，但 st-2 依赖已消失的 st-1 | 计数本身没错，是 P0-1 的派生现象；修了 P0-1 即消失 |

---

## 2. 修复方案（对齐产品哲学）

### P0-1 ELABORATE 拆分后重写下游依赖（最小改动、最高 ROI）

**改 `_resplit_subtask` / `elaborate` 循环**：每次把 `st` 拆成 `children`（id 形如 `st-1-1..st-1-N`，内部已串行）后，遍历 plan 中所有其它子任务，把其 `depends_on` 里指向 `st.id` 的项**重映射到子链尾节点**（`children[-1].id`），保证下游在整条子链完成后才就绪。
- 为什么映射到尾节点而非全部子节点：子链内部已串行（L679-681），尾节点完成 ⇒ 全链完成，语义最简且不破坏并行度判定。
- 落点：`elaborate` 在 `new_subtasks[idx:idx+1]=children` 后，加一段 `_remap_dependents(new_subtasks, old_id=st.id, new_id=children[-1].id)`。

> 单独修这一条，本任务大概率就能跑通——因为 worker 已把代码写对。

### P0-2 递归预算 + 规划熔断（把"框架崩溃"变"可读业务失败"）

1. **显式设 `recursion_limit`**：`brain_graph_config` 的 base 加 `recursion_limit`（建议 50，按里程碑/子任务规模可上调；当前 25 对"规划循环+多子任务+replan"明显不够）。
2. **规划失败熔断**：VALIDATE 连续 N 次（沿用 `MAX_PLAN_RETRY=3`）同类结构错误后，**不再去 confirm 蒙混**，而是：
   - 若 `auto_accept` 且 `plan_valid=False` → **不放行**，转人工确认（见 P0-3），无人工通道则 fail-fast 并报清晰原因（"计划依赖非法，自动校验 3 次未通过"），而非交给 recursion limit 兜底。
3. **replan 携带失败原因**：HANDLE_FAILURE→PLAN 重入时，把上次失败/校验 issue 注入 PLAN prompt，避免 LLM 原样重生成同一个坏计划（L508→L512→L515 又拆出同样的悬空依赖）。

### P0-3 auto_accept 不得放行非法计划（守住交互式确认闸门）

- `confirm_plan`：`auto_accept` 仅对 `plan_valid=True` 生效。`plan_valid=False` 时，即便 auto_accept 也**走 interrupt 等人工**（符合你"出及格选项+输入框让我填"的设计）——给用户两个选项：①按建议自动修正依赖后继续 ②人工编辑计划，外加自由输入框填补充约束。
- 修 P2-2 文案：L536 改为按 reason 输出 `_msg`（validation_failed / ultra 区分）。

### P1-1 scope 归属唯一化 + 被依赖产物自动入域

- PLAN/ELABORATE 产出后增加一道 **scope 归一**：同一文件的"创建/写"权限只归一个子任务；任一子任务 `depends_on` 的上游 `create_files`/`writable` 产物，自动并入本任务 `readable`（需读契约）或 `writable`（需改）。
- 直接消灭 L508 那类 "create_files 撞 writable 缺失 → empty_diff"。

### P1-2 L1 判定收敛单一事实源（你最在意的验证可信度）

- 新增统一判定：`l1_passed = compile_passed and tests_passed and not scope_violation and not empty_diff_when_changes_expected`。
- **dispatch 层禁止另写"通过"**：必须复用 executor 算出的 `l1_passed`，消除 L266 与 L112 的矛盾。
- **过滤模型拒答**：`raw_result` 命中 `Sorry, need more steps to process this request.` 等已知拒答/截断模式时，标 `llm_self_report=unavailable`，不混入 deterministic gate，并触发一次 worker 重试（而非当成"失败"扣分）。

### P2-1 同包依赖自动入 readable（让 mvn compile 不再必败）

- PLAN 切 Java scope 时，按 import / 同 package 推导，把被改文件引用的同模块类（如 Constants/StrFormatter/CharsetKit）纳入 `readable`，避免同模块编译因可读范围不全而注定失败。
- 取舍：完整 import 解析较重，**一期先做"同 package 全纳入 readable"的保守启发式**，覆盖本案；精确 import 图二期再说。

---

## 3. 并行编排（呼应你"独立则多容器并行"）

现状已有基础（`get_dispatch_batch` 依赖驱动调度 + `_decouple_independent_subtasks` 剥假依赖），本案的串行是 **P0-1 制造的假象**（st-1 被拆 + 依赖悬空，导致退化成单条派发）。
P0-1 修好后：st-1-1（建 NumberUtils）与 st-2（改 StringUtils 委托调用）—— 因 st-2 真依赖 NumberUtils 契约，**应保持串行**；但若未来有互不依赖的子任务，依赖驱动调度会自然并行派发到多沙箱。**本案不需要额外并行改造，P0-1 修复即让编排回归正确形态。**

---

## 4. 验证策略（按你"分级验证、风险定档"的偏好）

| 修复 | 改动性质 | 验证方式（轻→重） |
|---|---|---|
| P0-1 | 行为/数据变换 | 单测：构造"st-1 拆分→st-2 依赖重映射"，断言无悬空依赖（秒级） |
| P0-2 | 配置 + 熔断语义 | 单测 recursion_limit 已注入；熔断路由单测 |
| P0-3 | verify-gate/快路径语义 | 这类改 fast-path 语义 → 配一个真 E2E（auto_accept + 非法计划，断言不放行而是 fail-fast/interrupt），**E2E 要等到 verified terminal state 才宣布成功** |
| P1-1/P1-2 | 行为/leak-fix | scope 归一单测 + L1 判定单测（秒级），不必每条上 E2E |
| P2-1 | 行为 | scope 推导单测 |

最终重跑 **task 0f93f1fc 同款需求** 作为端到端回归，看是否 DONE。

---

## 5. 待确认疑问（请你拍板）

| 编号 | 疑问 | 选项 A（推荐） | 选项 B | 取舍 |
|---|---|---|---|---|
| Q1 | recursion_limit 设多少 | 50（固定） | 按 `max(25, 子任务数×6 + 里程碑×4)` 动态算 | A 简单可控；B 更贴合大任务但需调参，怕掩盖真死循环 |
| Q2 | auto_accept 遇非法计划怎么办 | 走 interrupt 等人工（出选项+输入框） | 直接 fail-fast 报错 | A 最贴你"交互式确认"哲学，但 API 纯自动场景会卡住；B 干脆但失去人工挽救机会。**倾向 A，但 auto_accept 且无 SSE 监听时降级为 B** |
| Q3 | P0-1 依赖重映射目标 | 子链尾节点（children[-1]） | 全部子节点 | A 语义最简、不伤并行度；B 冗余但更"显式" |
| Q4 | P2-1 scope 推导力度 | 一期"同 package 全纳入 readable"保守启发式 | 一期直接做 import 精确解析 | A 快、覆盖本案、低风险；B 更准但重、易引入解析 bug |
| Q5 | 版本号 | 这批是多缺陷修复，先 patch（0.6.x） | 攒齐这一整轮编排健壮性特性再 minor（0.7.0） | 按你"多批次中途 patch、整特性齐了才 minor"的习惯，**倾向先 patch** |
| Q6 | 落地顺序 | 先 P0-1 单独修+回归（验证"一改就通"），再批量 P0-2/P0-3/P1 | 六个一起改一次性提交 | A 风险可控、可早验证假设；B 快但难定位回归 |

---

## 6. 建议执行顺序（若你同意 Q6=A）

1. **P0-1**（依赖重映射）+ 单测 → 重跑 0f93f1fc 看是否一举通过
2. P0-2（recursion_limit + 熔断 + replan 带因）
3. P0-3（auto_accept 闸门 + P2-2 文案）
4. P1-1（scope 归一）+ P1-2（L1 单一事实源 + 拒答过滤）
5. P2-1（同包 readable）
6. 全量回归 + 三处版本号同步
