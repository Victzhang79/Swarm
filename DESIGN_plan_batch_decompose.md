# DESIGN: PLAN 超大需求按模块分批拆解

> 日期：2026-06-17
> 触发：e2e 压测完整预警平台 PRD（task 0f949f9e），tech_design 产出 file_plan=125 文件后，
> PLAN 单次 LLM 调用拆解 DAG 卡死 14 分钟无果，被迫 cancel。
> 状态：**草案，待 CTO 拍板后编码**

---

## 一、问题与机理（已定位到代码级）

### 现象
- ultra 需求（完整平台 PRD）→ tech_design 成功产出 **file_plan=125 文件**（fact_issues=0，方案能力没问题）
- → PLAN 节点 `await llm.ainvoke(...)` 单次调用，要 brain LLM 一次性把 125 文件拆成完整子任务 DAG
- → 卡死 14+ 分钟，无任何产出、无超时报错

### 根因（机理）
`models/router.py` brain LLM 配置：
- `streaming=True`、连接 `timeout=120s`、`stream_chunk_timeout=45s`（chunk 间看门狗）
- **只要 GLM 每 45s 内吐 ≥1 token 就永不超时**。生成 125 文件 DAG 的超长 JSON 时，GLM 持续缓慢吐 token → 14 分钟仍在续，任何超时都拦不住。

### 本质
让单次 LLM 调用拆解 125 文件的 ultra 需求 DAG **不现实**：
- 输出 JSON 极长（125 子任务 × 每个含 scope/contract/depends_on）→ 生成极慢、易截断、易 JSON 解析失败
- 单次上下文塞不下完整方案 + 125 文件的合理拆解推理

---

## 二、设计目标

PLAN 面对**超大 file_plan**（阈值待定，暂定 > 40 文件）时，不再单次拆全量 DAG，而是**按模块分组、逐组拆解、再合并**，每次 LLM 调用只处理一个模块的文件（可控规模），避免超长输出。

不改变：medium/simple 等中小需求的现有 PLAN 路径（单次拆解仍最优，零回归）。

---

## 三、方案（渐进明细）

### 3.1 触发条件
PLAN 节点开头判断：`complexity == ultra` 且 `len(file_plan) > BATCH_THRESHOLD`（暂定 40）→ 走分批路径；否则走现有单次路径。

### 3.2 分组策略（核心，三选一，见待确认疑问 Q1）
file_plan 每项已有 `{path, action, responsibility, depends_on}`。分组候选：
- **A. 按路径前缀/模块目录分组**：如 `ruoyi-system/.../alarm/task/*` 一组、`.../alarm/channel/*` 一组。确定性、零额外 LLM。
- **B. 让 tech_design 产出时就给 file_plan 标 `module` 字段**：改 tech_design schema，分组最准但要改上游。
- **C. 加一个轻量 LLM 分组调用**：把 125 文件路径列表丢给 LLM 只做"分组"（输出小），再逐组拆 DAG。

### 3.3 逐组拆解
对每个模块组：
- 调一次 PLAN LLM，只拆该组文件（10-30 文件，输出可控）
- 产出该组的子任务列表（含组内 depends_on）
- 组间依赖：按 file_plan 的 `depends_on` 跨组边，确定性串成组间顺序（如"实体组→Mapper组→Service组→Controller组→前端组"或按 PRD 模块依赖）

### 3.4 合并
- 各组子任务合并成总 DAG，组间用依赖序连接（复用现有 B3 依赖序拆分 + dispatch 的并行/串行调度）
- 子任务 id 全局唯一（组前缀 + 序号）

### 3.5 失败隔离
- 某组拆解失败/超时 → 该组降级（标记，不阻断其他组）+ 记录，便于人工介入
- 整体仍受 replan_count 熔断保护（复用 H2 修复）

---

## 四、待确认疑问（trade-off 表）

| # | 疑问 | 选项 | 倾向 | trade-off |
|---|------|------|------|-----------|
| Q1 | 分组策略 | A 路径前缀 / B tech_design 标 module / C 轻量 LLM 分组 | **A 起步**，不够再加 B | A 零额外 LLM、确定性，但依赖路径命名规律；B 最准但改上游 schema + 重打 tech_design；C 灵活但多一次 LLM |
| Q2 | 分批阈值 | 30 / 40 / 50 文件 | **40** | 太低则中等需求也分批(没必要)；太高则仍可能超长。需实测 GLM 单次能稳定拆多少 |
| Q3 | 组间依赖串法 | 按 file_plan depends_on 跨组边 / 按固定分层序(实体→DAO→Service→Controller→前端) | **depends_on 优先，回退固定分层序** | depends_on 准但 LLM 可能漏标；固定分层序稳但不够灵活 |
| Q4 | 是否需要"组级"人工确认点 | ultra 已有 CONFIRM 节点 | 复用现有 CONFIRM，不新增 | 避免每组都打断 |
| Q5 | 超大到什么程度该直接拒绝/要求拆需求 | 如 >200 文件提示用户分批提需求 | 设上限保护 | 防止 file_plan=500 这种根本跑不完的 |

---

## 五、影响面与回归保护

- **改动文件**：`brain/nodes/__init__.py`（plan 节点）+ 可能 `brain/planning_nodes.py`（若选 Q1-B 改 tech_design schema）
- **零回归要求**：medium/simple/小 ultra(≤阈值) 走原路径，现有 975 测试不受影响
- **新增测试**：分组逻辑单测（路径前缀分组正确）、分批触发阈值、组间依赖串接、合并后 DAG 完整性
- **e2e 验证**：修复后重跑完整 PRD，看能否拆出分组 DAG 并进入 dispatch（至少跑起来，不卡死）

---

## 六、不做什么（范围控制）

- 不改 brain LLM 的 stream/timeout 机制（那是通用配置，动它影响面大）
- 不追求"125 文件全部 worker 成功"（那是 worker 执行层的事，本 DESIGN 只解决 PLAN 拆解不卡死）
- 本批只让 ultra 超大需求**能拆解、能进 dispatch**，执行成功率是后续迭代
