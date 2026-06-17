# DESIGN: 多 worker 协同三要素 — 模型路由 / 共享契约 / 同文件并发

> 日期：2026-06-17　状态：**草案，待 CTO 拍板后编码**
> 来源：e2e task fd5470e0 暴露的三大真问题（详见 PROJECT_STATUS.md §四 T1/T2/T3）
> 本文一次覆盖三个强相关问题，因为它们共同决定"多个本地小模型 worker 如何协同把 ultra 需求做对"。

---

## 实施前 — CTO 拍板决策（2026-06-17 定稿）

| 疑问 | 决策 |
|------|------|
| Q-T1-1 契约产出位置 | **新增独立"契约设计"节点**（contract_design），不挤进 tech_design 阶段1。流程：tech_design(模块+数据模型) → contract_design(产出 shared_contract) → PLAN |
| Q-T1-2 契约生成模型 | **Brain 大模型直接生成**（契约是全局基石，用有全局视野的大模型求准，不交本地小模型） |
| Q-T2-1 complex 兜底链 | **先试同级另一主力(MiniMax-M2.7-Pro)，再降次级(Qwen3.6-27B-Saka)，最后 Qwen3.5-122B-A10B** |
| Q-T3-2 同文件并发 | **简单串行化**（有 writable 交集的子任务本批只取一个，其余下批；正确性优先于并行度） |

---



ultra 需求按模块分批 → 42 子任务 → 多 worker 并发执行。要做对，必须解决：
1. **谁来干**（T2 路由）：worker 该用本地小模型并行，但当前 complex 派给了云端 GLM、兜底 Kimi 403。
2. **怎么对齐**（T1 契约）：多 worker 各写各的，接口对不上（INotifyService 被两模块各建一次）。
3. **怎么不打架**（T3 并发）：两个 worker 同时改同一文件会互相覆盖。

三者关系：**契约先行(T1)** 定义了哪些是"共享文件"→ 这些文件不进 worker 并发写(T3)，由契约阶段一次定稿 → 路由(T2) 决定并发的 worker 用哪些本地模型。

---

## 一、T2 模型路由：worker 本地小模型并行

### 现状（config/settings.py L109-117 + models/router.py `_resolve_route`）
```
routing_trivial         = Qwen3.6-27B-Saka          (本地✓)
routing_trivial_fallback= Step-3.7-Flash            (云端?)
routing_medium          = MiniMax-M2.7-Pro          (本地✓)
routing_medium_fallback = Qwen3.5-122B-A10B-NVFP4   (本地✓)
routing_complex         = Pro/zai-org/GLM-5.1        (❌云端大模型)
routing_complex_fallback= moonshotai/Kimi-K2.6       (❌403 private)
```
`_resolve_route(difficulty)` 直接返回上面配置的 (primary, fallback)，worker 据此选模型。

### 设计意图（用户明确）
- worker 全部用【本地小模型】，绝不跑云端（云端只给 Brain 编排）。
- 本地模型梯队：
  - 主力并行：`Qwen3.6-40B-Claude-4.6-NVFP4`、`MiniMax-M2.7-Pro`（两个并行主力）
  - 次级：`Qwen3.6-27B-Saka-NVFP4`
  - 兜底：`Qwen3.5-122B-A10B-NVFP4`（上下文 64K，须控输入规模）
- max_workers=4 = 本地 4 个模型槽；测试阶段可减。

### 方案
1. **改路由配置**（确定性，低风险）：
   ```
   routing_trivial          = Qwen3.6-27B-Saka-NVFP4
   routing_trivial_fallback = Qwen3.5-122B-A10B-NVFP4
   routing_medium           = MiniMax-M2.7-Pro
   routing_medium_fallback  = Qwen3.6-40B-Claude-4.6-NVFP4
   routing_complex          = Qwen3.6-40B-Claude-4.6-NVFP4   (本地,不再云端)
   routing_complex_fallback = MiniMax-M2.7-Pro → Qwen3.6-27B-Saka → Qwen3.5-122B-A10B
   ```
   移除所有 moonshotai/Kimi-K2.6（403）。
2. **并发分派轮转**（dispatch）：同一批并发 worker 轮转分配到不同本地主力模型
   （worker A→Qwen3.6-40B-Claude，worker B→MiniMax-M2.7-Pro，分散本地推理负载）。
3. **多级兜底链**：主→次→兜底全部本地。`get_llm_for_subtask` 的 with_fallbacks 串多级。
4. **WebUI 呈现**：路由配置可视化可编辑（落库+保存即 reload，符合用户配置偏好）；
   任务监控页显示每个 worker 当前用哪个模型 + 本地模型槽占用。

### 待确认（T2）
- Q-T2-1：兜底链顺序——complex 失败后先试同级另一主力(MiniMax)，还是直接降次级(Saka)？倾向先同级。
- Q-T2-2：64K 的 Qwen3.5-122B 兜底时，是否要先做输入裁剪（worker prompt 超 64K 时压缩 readable 上下文）？
- Q-T2-3：轮转分配按什么键？子任务序号取模 / 真实模型槽空闲状态（需查本地网关是否暴露负载）？

---

## 二、T1 共享契约（shared_contract）契约先行

### 问题本质
多个本地小模型 worker 独立拆、独立写，**无法天然对齐接口**。铁证：
- INotifyService 被 st-2(channel/service/) 和 st-29(engine/dispatch/) 各建一次，签名可能不同。
- NotifyServiceFactory 与 NotifyStrategyFactory 功能重复。
- st-26(ServiceImpl) 依赖的 Mapper 接口签名由别的 worker 定，对不上就编译失败。

### 方案：契约先行（Brain 定契约 → worker 遵守）
1. **tech_design 阶段1 产出共享契约**（已产出 modules + data_model，再加 shared_contract）：
   - 跨模块共享的：核心接口签名(INotifyService 的方法签名)、DTO 字段、常量、API 路径规范、命名约定。
   - 这是【全局唯一】的一份，由 Brain（大模型，有全局视野）一次定稿。
2. **契约文件单独成"契约子任务"，最先执行**（不进并发写）：
   - 把共享接口/DTO/常量作为一个【契约子任务 st-contract】，串行最先跑，产出落盘。
   - 所有其他子任务 depends_on 它 → 执行时这些契约文件已存在，worker 只读引用。
3. **契约作为只读上下文注入每个 worker**：
   - PLAN 分批 + dispatch 时，把契约内容注入每个 worker 的 readable + prompt，
     "你必须遵守这份接口契约，不要自己另建同名接口"。
4. **合并时校验契约一致性**：
   - 复用已有 dedupe_file_plan(P5 同名去重) + merge_engine。
   - 新增：检测多个子任务是否声明了契约里已定的接口/类 → 去重 + 告警。

### 待确认（T1）
- Q-T1-1：契约由 tech_design 阶段1 产出，还是新增独立"契约设计"节点？倾向 阶段1 加 shared_contract 字段（少改流程）。
- Q-T1-2：契约子任务用什么模型？倾向本地主力(Qwen3.6-40B-Claude，契约是接口骨架不复杂)，但它是全局基石，是否值得用 Brain 直接生成（更准）？
- Q-T1-3：契约粒度——只定"跨模块共享"的，还是每个模块内的接口也定？倾向只定跨模块共享（模块内的归 worker 垂直切片自决）。

---

## 三、T3 同文件并发编辑防冲突

### 问题
`get_dispatch_batch` 取依赖满足的子任务 `[:max_concurrent]` 并行，**没检查这批的 writable 文件是否有交集**。
两个 worker 同时改同一文件 → 各自在自己沙箱改 → pull-back 互相覆盖 → 丢改动。

### 方案
1. **派发前文件交集检测**（get_dispatch_batch 增强）：
   - 选并发批次时，保证批内子任务的 writable/create_files 集合【两两无交集】。
   - 有交集的子任务：本批只取一个，其余留到下一批（串行化有冲突的）。
2. **理想态固化**：按模块垂直切片(已做) → 同模块文件在同 worker → 跨模块共享文件由契约阶段(T1)定稿不进并发。
   两者结合后，并发 worker 天然改不同文件。
3. **合并阶段兜底**：merge_engine 已有冲突检测；同文件被多 worker 改时按依赖序合并 + 冲突告警。

### 待确认（T3）
- Q-T3-1：文件交集检测放 get_dispatch_batch（types.py）还是 dispatch 层？倾向 get_dispatch_batch（离并发决策最近）。
- Q-T3-2：有交集时串行化，会降低并行度——可接受（正确性优先于速度），还是要更细（按文件加锁让不冲突的部分继续并行）？倾向先简单串行化。

---

## 四、实施顺序（拍板后）

1. **T2 路由**（确定性配置，最先，立竿见影）：改 settings + dispatch 轮转 + WebUI。单测 + e2e 复测看 worker 全本地。
2. **T1 契约**（架构，核心）：tech_design 阶段1 加 shared_contract + 契约子任务前置 + 注入 worker + 合并校验。
3. **T3 并发**（依赖 T1）：get_dispatch_batch 文件交集检测。
4. e2e 总验：完整 PRD 跑到 worker 全本地、契约一致、无同文件冲突、真实产出 + L1/L2 过。

---

## 五、不做什么（范围控制）
- 不改 Brain 编排用云端（云端只给大脑，符合范式）。
- 本批不追求 PRD 100% 覆盖（前端/SQL/代码生成器是 T5，后续迭代）。
- 不引入分布式锁（同文件并发用"派发期错开"解决，不在文件系统加锁）。

---

## 六、影响面与回归
- 改动：config/settings.py(路由)、models/router.py(兜底链)、brain/nodes/dispatch.py(轮转+交集)、
  brain/planning_nodes.py(阶段1 契约)、brain/plan_batch.py(契约子任务/合并校验)、types.py(get_dispatch_batch)、
  WebUI(api/static + 路由配置端点)。
- 零回归要求：中小需求/非 ultra 路径行为不变；现有 991 测全过 + 各项新增单测。
- e2e：RuoYi-E2E 完整 PRD 重跑，对照 PROJECT_STATUS §五 验证方法。
