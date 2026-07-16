# round64 复盘 + 治本登记册（2026-07-16）

> task=f1e0f7b5-3be8-438e-8c07-fef2dc5588a6 · FAILED@PLAN（22:32，起跑 20:51，~101min）
> 零执行、零沙箱、零 worker 烧钱——全部消耗在规划期 3 次 PLAN 循环。
> ★死轮已沉淀离线 fixture：`cassettes/f1e0f7b5-….json`（107 子任务/170 文件/base=0d42679）
> ★LLM 全程录制在 `cassettes/round64/llm-96074.jsonl`（analyze/tech_design/contract_design/
> extract_requirements 等全 brain 节点，T11 首轮兑现）

## 一、死因定案（已亲核代码，非推测）

**G1 模块 coherence 闸三验三拒 → CONFIRM 自动拒绝 → FAILED。**
违例恒为同一条：`ruoyi-admin / ruoyi-alarm / ruoyi-system` 各自 ↔ `[<模块目录>, 'sql']` 双物理根。

根因链（每环已读源码亲核）：
1. PRD 要求 DDL 脚本；RuoYi 惯例 sql 落**仓库顶层 `sql/`**（合法棕地布局）。GLM 三轮 plan
   都把 `sql/xxx.sql` 的 file_plan.module 标成对应功能模块（语义上完全合理）。
2. `contract_utils._resolve_module_dirs` 的 fp_ambiguous 判定（contract_utils.py:565-568）：
   `_code_module_root("sql/xxx.sql")` 找不到源码布局段 → None → **回退顶层目录 "sql"**
   （该回退是 Task#9 silent-hunter #1 整改为 flat/纯脚本项目加的栈中立兜底）→
   模块 roots = {"ruoyi-admin","sql"} → 违① → G1 硬打回。
3. issues 反馈让 LLM「把全部文件归到同一模块目录」——对 RuoYi 布局这是**结构性不可满足**
   （sql 按惯例就住顶层），LLM 重试永不收敛 → 3 连拒 → FAILED。

**定性：两条治本互拆（round60 同型）**——silent-hunter #1 的顶层目录回退（防 flat 项目漏判）
把「模块合法拥有仓库顶层辅助文件（sql/docs/scripts）」误判成「模块散落多物理根」。
G1 闸本体行为正确（宁 FAILED 不派发；对比 round41 同病根烧 90st 才死）。

## 二、治本方向（待 test-first + 对抗双复核）

**T1（主治·处方草案）：证据强度分层——fallback 根不与 code 根构成歧义。**
fp 多根判定中：若 ≥1 根来自 `_code_module_root`（真源码布局证据），则仅由顶层回退产生的
根（sql/docs 等无源码布局的辅助文件）**不计入歧义**——模块物理根取 code 根，辅助文件随
file_plan 落盘即可（不参与构建，不需要 pom；round19/41 的孤儿 sql 通道早已能收口）。
flat/纯脚本项目所有根都来自回退 → 多根仍触发（保住 silent-hunter #1 本意）。
判据 disk-independent、栈中立、确定性——不违 round59「判据用结构不用状态」铁律。

**T2（配套核）：G1 issues 反馈的可修正性**——反馈文案给的两个选项对本 case 都不可行；
T1 治掉后此路径不应再触发，但需复核反馈是否还有其它「让 LLM 做不可能的事」的措辞。

**T3（观察遗留·非本轮阻断）：**
- 重试 1 只用 2min 近原样重放（21:56→21:58）vs 重试 2 完整重产 20min——两种重试路径差异
  是否 by design、issues 注入在快速路径是否真生效，值得取证。
- 旧 task a32c5862 的周期 401 轮询残留（非本任务，卫生项）。

## 三、验证方式

- RED：用 fixture `cassettes/f1e0f7b5-….json` 走 `cassette_replay.py` 复现 G1 打回（秒级零云端）。
- GREEN：治后 replay 同 plan 应 resolved=模块 code 根、无 ambiguous、G1 通过。
- 全量套件 + 对抗双复核（code-reviewer + silent-failure-hunter，点名「flat 项目回退还工作吗」
  「多 code 根仍打回吗」两个断裂方向）+ revert-check + 本地提交（local-commit-only 默认模式）。

## 四、本轮已兑现的治本（不白跑的部分）

- ★G1 闸 live 首触发且止损行为全对：拦下不可执行 plan，零 worker 浪费（round41 同型病根
  当年烧 90st 执行期才死）。
- ★T11 录制全节点兑现：analyze/tech_design/contract_design/extract_requirements 首次入带。
- ★round62 系兑现：模块=物理构建单元口径（5 模块无爆炸）、Task1 file_plan 跨模块归位、
  Task5 幻影路径归一（40/59 条）、C1 契约对账零打回（round39/42/43/44 连环死点首次一次通过）、
  R48b-1 无主硬符号确定性收编、A7 存量对账 23/104 有料。
- worker 侧 T7/T8/T9 未获检验机会（没到执行期）——round64b 顺延验证。

---

# 五、三路深查定案（2026-07-16 深夜，两 agent 亲核 cassette 全量 LLM 产出 + 逐行通读 3822 行日志）

## 完整因果链（修正早前"重试1单发修复"误判：seq38/57 是 P1 覆盖补齐，非 G1 修复）

1. **源头=tech_design phase2 提示词两规则无豁免叠加**：「DDL 落已有 sql/ 目录（磁盘事实优先）」
   ×「所有文件 module 填当前模块名」→ 3 模块各认领顶层 sql/*.sql（seq2/4/5 原文实锤）。
   plan 阶段 file_plan 逐字继承。
2. **判定层过度触发**：resolver 对无布局信号文件回退顶层目录当物理根 → G1 违① 硬打回
   （T1 已治：证据强度分层后 sql 归属模块合法化，tech_design 提示词无需改——辅助文件
   打模块标签本来就语义正确）。
3. **反馈路由结构性断裂**：G1 issues 只注入 plan_batch user prompt（重试2 全 11 批），但
   plan_batch (a) 输出 schema 无 module/file_plan 字段 (b) P4 规则强制"不要改前缀"
   (c) 文件清单本身把 sql 钉死在模块下 → 反馈要求它做输出契约上无法表达的事 → 必然不收敛。
4. **重试策略无收敛判据**：pass1/pass3 走 P1 外科（只补覆盖，结构性问题注定空转，白耗 2 次
   重试额度）；pass2 全量重产 33min 输出 sql 处置逐字节相同；三次违例签名完全一致却无熔断
   → 68min/~50% plan_batch token 纯浪费。
5. 云端产出质量普查：plan_batch 22 次 0 截断 0 error；elaborate 19 次抽查无幻觉；
   extract_requirements 104 条 0 幻觉 7 域全覆盖；contract 与 MERGE 全对账一致。
   模型行为本身健康——死的全是编排/判定层。

## 新登记 task（死因之外）

- **T3（高·阻断 round65）结构性校验失败的重试治理**：①G1 类结构 issue 不走 P1 外科路径
  ②同违例签名连续两轮 → 确定性熔断 fail-fast（省 33min 全量重产）③设计遗留：结构反馈
  应回灌 file_plan 所有者层（tech_design）而非 plan_batch——本轮先做 ①②确定性止损。
- **T4（高·先调查）契约 name↔file_plan 命名漂移**：契约条目只有 name+module 无落盘路径，
  56 个中 30 个 name 对不上 file_plan basename（AlarmSimpleRequest↔SimpleNotifyRequest、
  AlarmComposeUtil 无文件）= R62-Task5 幻影路径复发源头。先核 R63-T4 defined_in 钉死机制
  的覆盖面为何没兜住，再定处方。
- **T5（中）tech_design seq6 退化空转**：500s 撞墙 GeneratorExit、28841 chunks 零可用输出
  （重试 61s/3367 chunks 即成）——疑似复读退化，核 R63-T7 退化探测为何未覆盖 brain 流。
- **T6（小）录制补终止元数据**：58 行 cassette stop/finish_reason 全 None，无法事后判截断。
- **T7（小批）**：①启动期路由 4 档含不可达 ThinkingCap-Qwen3.6-27B（soak 却全绿，疑能力库
  脏行/名称映射，用 model_swap_audit.py 查）②ledger 预留 48fa3b51 TTL 泄漏（取消调用未结算）
  ③FAILED@PLAN 终态无结构化 degraded_summary（只有 error 字符串，应记 retry 轮次/违例明细）。

## 观察项（登记不阻断，诚实划界）

- G2 readable 成环 42→74 对、T5 模块互消费环 2→4 对（全量重产后恶化）：advisory 告警已
  surface，plan 耦合质量信号，待 T3③ 反馈路由重设计一并考虑。
- st-1/st-13 拆后仍超预算 147456：颗粒度信号，已有"需人工重新切分"告警出口。
- 残留浏览器标签页对旧 task 401 轮询 273 次（终态后仍持续）：前端对 401 应停轮询，非本轮症。
- 多写者串行化 29 条/contract 未引用 32 条：已有串行化兜底，plan 质量信号。
