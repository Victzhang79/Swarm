# Swarm 架构演进路线图（生产化前置项）

> 维护：CTO ｜ 基线：v0.6.x ｜ 性质：架构级改动，需 DESIGN DOC 先行，**非当前单机/试点阶段的 ROI 项**
>
> 这些条目来自架构走查（REVISED）与 Brain/Worker audit，经复核确认是**真实的架构约束**，
> 但在当前单机部署 + 试点验证阶段修复它们是**负 ROI**（回归风险 > 当前收益）。
> 记录在此，按生产化路线图排期，避免遗忘，也避免在错误的阶段提前优化。

---

## P0 — 水平扩展前必须解决（多副本部署的硬阻塞）

### A1. 单进程内存态 → 状态外置化（REVISED 12.1）✅ 完成 @ v0.7.0
- **现状**：热沙箱池、LangGraph MemorySaver checkpointer、若干调度器都是**进程内内存态**。
- **约束**：多副本（多 uvicorn worker / 多容器）部署时，状态不共享 → interrupt/resume 跨副本失效、
  沙箱池各副本各管各的、孤儿沙箱归属混乱。
- **DESIGN DOC 需回答**：
  - checkpointer 迁移到 PG（langgraph-checkpoint-postgres）还是 Redis？
  - 沙箱池改为外部协调（Redis 租约 + 实例标签，复用本轮 12.2 验证的 CubeSandbox metadata 能力）还是单独的池服务？
  - 调度器（task/decay/kb_update/consistency）多副本下的 leader election？
- **前置依赖**：CubeSandbox metadata 过滤已实测可行（见 supermemory，2026-06），是沙箱归属方案的基础。

### A2. 沙箱级隔离 enforcement（REVISED 12.10 / 12.18）
- **现状**：FileScope 越权拦截在**工具层**（scope_guard）；run_command 白名单是**应用层**。
- **约束**：多租户/不可信代码场景下，应用层拦截可被绕过（沙箱内进程直接 syscall）。
- **DESIGN DOC 需回答**：沙箱网络隔离策略、文件系统 chroot/挂载限制、能否复用 CubeSandbox 原生隔离能力。
- **触发条件**：开放给外部不可信用户提交任务时。当前内部试点不紧急。

---

## P1 — 可维护性 / 长期健康

### B1. brain/nodes.py 单体拆分（REVISED 12.8）
- **现状**：~2350 行，14 个节点 + 大量辅助函数集中一个文件。
- **方案草案**：按节点域拆 `brain/nodes/`（analyze.py / plan.py / dispatch.py / verify.py / learn.py / merge.py + shared.py）。
- **风险**：纯重构，回归风险 > 收益；需充分测试护航（当前 688 测试是基础，但节点间 import 关系复杂）。
- **建议时机**：有一次"反正要大改 brain"的功能迭代时顺带拆，不单独为拆而拆。
- **已有先例**：参考已沉淀的 `python-module-splitting` / `fastapi-monolith-router-split` skill（AST 脚本化提取 + mock 合约保持）。

### B2. 全子系统共进程 → 服务边界（REVISED 12.20）
- **现状**：API / Brain 编排 / Worker 执行 / 知识库 / 记忆 全在一个进程。
- **长期**：Worker 执行（重、可能 OOM）与 API（要稳）应进程/服务隔离。
- **建议时机**：A1 状态外置化之后的自然延伸。

---

## 已确认【无需处理】的架构相关项（复核结论，留档防重复讨论）

| 项 | 结论 |
|----|------|
| L1 双 pipeline (#5/#29) | 分阶段设计（Phase3 循环闸门 + Phase4 最终复核），已加 l1_phase 标记，非 bug |
| L1.4 自检不硬阻断 (#28) | 设计选择（自检为弱信号），已加 skipped 标记区分异常跳过 |
| lint gate (#27) | 环境变量可配是有意设计，已加降级日志 |
| 升级阶梯 off-by-one (#7/#26) | 有意设计：retry×2 + alternate×1，非 bug |

---

## 决策原则（给未来的自己）

1. **不在试点阶段做生产级架构改造** —— 先验证产品价值，再投入水平扩展工程。
2. **架构改动必须 DESIGN DOC 先行 + 渐进明细 + 待确认疑问** —— 不直接动手。
3. **拆分/重构搭功能迭代的便车** —— 纯重构单独立项往往负 ROI。
4. A1（状态外置）是其它 P0/P1 的前置 —— 要做先做它。

---

*本路线图随架构演进更新。当前 v0.6.x 阶段：以上均不动代码。*
