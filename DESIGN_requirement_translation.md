# DESIGN: 需求转化 / 技术设计前置阶段

> 状态：草案待拍板 | 触发：用户指出"我喂的是技术方案不是产品需求"——系统缺"产品语言→技术方案"的转化层。
> 核心约束：真实产品经理只说"管一下公司的设备"，不说表/字段/类名/分层。转化层要自己补全这些。

## 0. 关键调研发现（已走读确认）

- ✅ **tech_design 节点已存在**（planning_nodes.py:408）——"资深技术负责人产出技术方案"，已能产架构/数据模型/流程/契约/风险。
- ❌ **缺口1：几乎是死代码**。`after_analyze` 只路由 clarify/plan（graph.py:79），tech_design 只在 `clarify→assess→tech_design` 路径触发；auto_accept/不澄清的需求 ANALYZE 直达 PLAN，**永不经过 tech_design**。
- ❌ **缺口2：产出没喂给 PLAN**。PLAN（nodes/__init__.py）完全不读 `state["tech_design"]`。
- ❌ **缺口3：产出粒度不对**。tech_design 给"架构概述/数据模型描述"，但【没给文件级方案】——没说"要建 BizDevice.java/BizDeviceMapper.java/... 各文件路径+职责"，PLAN 仍要自己猜要建哪些文件。
- ❌ **缺口4：触发判据错**。assess 判 needs_tech_design 只看 complex/新建；产品需求"管设备"会判 medium，不触发。

## 1. 设计目标

产品需求（"管一下设备"，零表零字段零类名）→ 转化层自己设计出：要建什么表+字段、按项目规范要建/改哪些文件（含完整路径）+ 每个文件职责 → 喂给 PLAN，PLAN 据此定 scope。

## 2. 核心方案：激活并改造 tech_design 为"需求转化层"（不新造节点）

不引入新节点，把现有 tech_design 补全成真正的转化层，三处改：

### 改动1：让产品需求能触发（修触发判据）
- 不再只看 complex/新建。新增判据：**需求是"功能性新增/修改"且未点名文件**（产品经理式）→ 也走 tech_design。
- 但守卫：trivial/单点改动（"改个文案""修typo"）不触发——避免给简单任务套重流程。

### 改动2：产出"文件级技术方案"（补关键缺口）
tech_design 输出新增字段 `file_plan`：每个要建/改的文件 {path, action(create/modify), responsibility, depends_on_files}。这是连接"业务设计"和"PLAN scope"的桥。

### 改动3：tech_design 喂给 PLAN（打通数据流）
PLAN prompt 注入 tech_design 的 file_plan + 数据模型 + 契约。PLAN 不再从零猜文件，而是据技术方案定 scope。

## 3. 待确认疑问 / trade-off 表（请拍板）

| # | 决策点 | 选项 | trade-off | 我的倾向 |
|---|---|---|---|---|
| Q1 | 改造现有 tech_design 还是新建节点 | 改造 / 新建 | 改造复用已有 prompt/评审/契约机制，不增图复杂度；新建更干净但重复 | **改造**（tech_design 本就是干这个的，只是没接通） |
| Q2 | 触发判据 | 仅 complex / 功能性新增都触发 / LLM判 | 太窄漏掉产品需求；太宽给简单任务套重流程 | **功能性"新增模块/新增功能"且未点名文件 → 触发**，trivial/单点改动不触发 |
| Q3 | file_plan 谁来定文件路径 | LLM 凭知识库 / 正则模板 / 混合 | LLM 准但可能瞎编路径；模板死板 | **LLM 基于知识库（codegraph 已知项目结构）设计**，参照同类已有文件的路径规律 |
| Q4 | 转化层失败怎么办 | 阻断 / 降级直接 PLAN | 阻断太脆；降级保主流程 | **降级直接 PLAN**（与现有 except 一致，不阻断主流程） |
| Q5 | auto_accept 模式要不要人工评审 file_plan | 评审 / 跳过 | 评审更稳但破坏 auto；跳过快但可能方案错 | **auto_accept 跳过评审**，方案错由后续 L1/L2 闸门兜底 |
| Q6 | 表/字段设计要落 DDL 吗 | 落建表SQL / 只描述 | 落 SQL 更完整但 RuoYi 表由 gen 管；只描述够 PLAN 用 | **先只描述数据模型**（字段+类型），DDL 留 ROADMAP |

## 4. 验证标准（真实产品需求闭环）

用**真·产品经理式需求**（我不再自己编技术方案）：
> "我们经常不知道公司的设备在谁手里、什么状态，想要个功能管一下设备。"

期望系统自己走通：
1. tech_design 触发 → 设计出 biz_device 表（自己定字段）+ 文件级方案（自己定要建 6 个 RuoYi 分层文件的路径+职责）
2. PLAN 据 file_plan 定 scope（不用我喂文件名）
3. worker 实现 → 端到端 DONE，6 文件各层正确

**这才是真闭环**：产品话进，可运行的分层代码出，中间的技术设计系统自己做。

## 5. 实施顺序
1. tech_design 输出加 file_plan 字段（改 prompt + 解析）
2. 修触发判据（after_analyze 增加"产品式功能需求→tech_design"路由 + 守卫）
3. PLAN 注入 tech_design
4. 真实产品需求验证闭环

