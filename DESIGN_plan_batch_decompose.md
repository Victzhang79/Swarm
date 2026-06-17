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

## 四、已拍板决策（2026-06-17 CTO 定稿）

| # | 决策 | 落地 |
|---|------|------|
| Q1 | **A+B 都做**：路径前缀分组(确定性,主) + tech_design 产出标 `module` 字段(更准)。C 留底座但用**本地小模型**做分组——因对小模型产出不完全信任，小模型只做"分组"这种低风险结构化映射，且必须过确定性校验(分组结果文件总数=原 file_plan 数,无遗漏无重复),不通过则回退 A。 | tech_design schema 加 `module`；plan 分组函数 `_group_file_plan(file_plan)`：优先用 module 字段，缺失回退路径前缀；C 作为可选增强后置 |
| Q2 | **按 10% 比例分批**(不是固定阈值)：N 文件 → 每批 ceil(N/10) 个，约 10 批 + 余数批。每批拆解前后打日志：**当前批次/总批次、完成百分比、本批云端 LLM 调用耗时**。 | `_batch_size = max(1, ceil(len(file_plan)/10))`；逐批 LLM 拆解，进度日志 `[PLAN-BATCH] 批 3/11 (27%) 文件数=12 LLM耗时=43.2s` |
| Q3 | **depends_on 优先，回退固定分层序**(实体→DAO/Mapper→Service→Controller→前端) | 组间排序：先按 file_plan 的 depends_on 跨组边拓扑排序；无依赖信息的组按分层序兜底 |
| Q4 | **复用现有 CONFIRM 节点**，不新增组级确认 | ultra 已进 CONFIRM |
| Q5 | **>200 文件 → 建立串行主任务**：主任务间相互依赖，A 执行完且**产出合格(有产出+L1/L2过)**才走关联串行 B。单主任务内仍走 10% 分批。 | 新增"主任务编排"层：file_plan>200 时按大模块(PRD 顶层模块)切成多个串行主任务，主任务 B 的 dispatch 前置校验主任务 A 已 DONE 且产出落盘 |

### 工程类比（为什么这么设计）
人工完成此类项目：389 文件 / 380 提交 / 净增 2 万行——靠的就是**分模块推进、串行依赖、做完一段验一段**。125 文件变动不可怕，关键是**门控**：可控批次 + 每批独立产出 + 做完核对 + 依赖串接。这正是把"一个 agent 一次干完"升级为"像 CTO 带团队分工核对"的生产级编排。

### Q5 串行主任务（>200 文件）补充设计
```
file_plan > 200
  │
  ├─ 按 PRD 顶层模块(预警核心/预警引擎/值班排班/认证授权/系统管理/开发工具)切成 M 个主任务
  │   每个主任务 = 一个完整可验收的子系统
  │
  ├─ 主任务间依赖排序(如 认证授权 → 预警核心 → 预警引擎 → ...)
  │
  └─ 串行执行：
       主任务A 完整跑(分批PLAN→dispatch→L1/L2) → DONE 且产出 commit 落盘验证
         │ (产出合格闸门：有 diff + L1过 + git 落盘)
         ↓
       主任务B 开始(可引用A的产出,事实库已更新)
         ↓ ...
```
本批先实现 **单主任务内的 10% 分批拆解**（解决 125 文件卡死）；串行主任务编排作为 Q5 第二阶段（>200 触发），先留接口与判定点。

---

## 五、实施分解（像带团队一样分模块核对）

> 我会按模块逐个实施 + 每模块独立测试 + 核对，不一次性堆。顺序：

1. **M-1 分组函数** `_group_file_plan`：module 字段优先 + 路径前缀回退。单测：分组无遗漏/无重复、覆盖全部文件。
2. **M-2 tech_design schema 加 module**：file_plan 项加 `module` 字段 + prompt 引导。单测：schema 容忍缺失(向后兼容)。
3. **M-3 分批拆解主逻辑**：ultra 且 file_plan 大 → 10% 分批，逐批 LLM 拆解 + 进度日志。单测：批次数计算、进度日志格式。
4. **M-4 组间依赖串接 + 合并**：depends_on 拓扑序 + 分层序回退，子任务 id 全局唯一，合并成总 DAG。单测：拓扑排序正确、id 唯一。
5. **M-5 失败隔离 + 熔断**：某批失败降级不阻断 + 复用 replan_count。
6. **M-6 e2e 重验**：重跑完整 PRD，看能否分批拆出 DAG 并进 dispatch(不卡死)。
7. **(第二阶段) M-7 Q5 串行主任务**：>200 文件触发，先留判定点与接口。

---

## 六、影响面与回归保护

- **改动文件**：`brain/nodes/__init__.py`(plan 分批) + `brain/planning_nodes.py`(tech_design schema module) + 可能新增 `brain/plan_batch.py`(分组/分批/合并逻辑独立模块)
- **零回归要求**：medium/simple/小 ultra(文件数小,单批即全部) 走等价原路径，现有 975 测试不受影响
- **新增测试**：分组无遗漏、批次计算、拓扑串接、id 唯一、合并 DAG 完整性、向后兼容(无 module 字段)
- **e2e 验证**：重跑完整 PRD，PLAN 不再卡死，分批拆出 DAG 进入 dispatch

---

## 七、不做什么（范围控制）

- 不改 brain LLM 的 stream/timeout 通用机制
- 本批只解决 **PLAN 拆解不卡死 + 能进 dispatch**；worker 125 文件全部执行成功是后续迭代
- C(本地小模型分组)只留底座，不在本批启用(对小模型此阶段产出不完全信任)
- Q5 串行主任务本批只留判定点接口，完整实现是第二阶段

---

## 八、e2e 重验发现（2026-06-17 M-6）：同根瓶颈的上游——tech_design 也会卡

PLAN 分批实现 + 单测通过（985 passed）后重跑完整 PRD e2e，**TECH_DESIGN 节点卡死 12 分钟无果**
（第一次 8 分钟能出，这次卡住——同一个 tech_design 单次 LLM 调用，brain GLM 生成 125 文件
file_plan 的超长 JSON 时不稳定，0.1% cpu = 纯等 LLM IO）。

**洞察**：ultra 需求的真实瓶颈是【整条链路上每个"单次 LLM 生成超长输出"的节点】：
- 坎1：tech_design 出 125 文件 file_plan（超长 JSON）← 本次卡这里
- 坎2：PLAN 拆 125 文件 DAG（已用 10% 分批解决）

只修 PLAN 是治标。**tech_design 产出 file_plan 也应分模块/分阶段**才是治本。

### 待定方向（下一步）
- **方向 A**：tech_design 也分阶段——先让 LLM 只产出【模块清单 + 数据模型】（短输出），
  再按模块逐个产出该模块的 file_plan（每次短输出）。最彻底但改 tech_design 较大。
- **方向 B**：tech_design 产出 file_plan 时限制粒度——只产出【模块级方案 + 每模块文件数估计】，
  不展开到 125 个具体文件路径；具体文件路径下放到 PLAN 分批时按模块现推。
- **方向 C**：换更快/更稳的 brain 模型跑 ultra 的 tech_design（运维侧，非架构）。
- **方向D**：给 brain LLM 超长输出加硬上限 + 分段续写（max_tokens 分段 continuation）。

---

## 九、e2e 实证(task fd5470e0)：分批跑通了但暴露拆解质量回退（待治本）

两阶段 tech_design(6模块86文件) + PLAN 10%分批(42子任务,0卡死) 全程跑通进 dispatch，
worker 开始并发执行——**"不卡死"目标达成**。但 VALIDATE_PLAN 软建议 + st-26 失败(600s超时空产出)
暴露分批引入的**拆解质量回退**（分批为了规模可控，丢了 PLAN 原有的垂直切片+全局协调）：

| # | 问题 | 根因 |
|---|------|------|
| P1 | 违反垂直切片：一个功能拆成 Entity→Mapper→Service→Impl→XML→Controller 6 子任务 | 每批 9 文件被 LLM 按技术层水平切，丢"一功能=一子任务" |
| P2 | 跨批依赖丢失：st-26(ServiceImpl)依赖的 Mapper/实体在别批，depends_on 没建→worker 找不到→600s超时空产出 | merge 只加机械批间串行，没按真实语义依赖排 |
| P3 | 新模块基础设施缺失：ruoyi-alarm 全新 maven 模块无 pom.xml/目录→该模块全部编译失败 | 无"模块脚手架"前置子任务 |
| P4 | 文件路径前缀不一致(src/ vs ruoyi-alarm/src/)→编译失败 | 各批独立拆，路径未统一 |
| P5 | 重复创建：INotifyService/工厂类被两个批各建一次 | 跨批无全局符号表 |
| P6 | acceptance_criteria 全空 | 分批 prompt 没要求验收标准 |

### 治本方向（待拍板）
核心：**分批要按"垂直功能切片"分组，而非"技术层水平切片"**。即：
- **分组键改为"功能模块"而非"文件批次"**：alarm-task 这个功能的 Entity+Mapper+Service+Controller
  应在【同一个子任务/同一批】，而非散落多批。tech_design 的 module 字段正是天然的垂直切片边界。
- **A. 按 module 分批(而非 10% 机械切)**：每个 tech_design 模块 = 一批，批内 LLM 拆该模块为垂直切片子任务。
  模块即垂直边界，天然避免 P1/P2/P5。配合模块脚手架前置(P3)、路径规范化(P4)、要求验收标准(P6)。
- **B. 模块依赖排序**：tech_design 阶段1 已产出模块 depends_on，按它排批间序(P2)。
- **C. 全局符号表**：合并时检测重复创建的同名文件，去重(P5)。

---

## 六、不做什么（范围控制）

- 不改 brain LLM 的 stream/timeout 机制（那是通用配置，动它影响面大）
- 不追求"125 文件全部 worker 成功"（那是 worker 执行层的事，本 DESIGN 只解决 PLAN 拆解不卡死）
- 本批只让 ultra 超大需求**能拆解、能进 dispatch**，执行成功率是后续迭代
