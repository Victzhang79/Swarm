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

### A2. 沙箱级隔离 enforcement（REVISED 12.10 / 12.18）✅ 完成 @ v0.7.1
- **现状**：FileScope 越权拦截在**工具层**（scope_guard）；run_command 白名单是**应用层**。
- **约束**：多租户/不可信代码场景下，应用层拦截可被绕过（沙箱内进程直接 syscall）。
- **DESIGN DOC 需回答**：沙箱网络隔离策略、文件系统 chroot/挂载限制、能否复用 CubeSandbox 原生隔离能力。
- **触发条件**：开放给外部不可信用户提交任务时。当前内部试点不紧急。

---

## P1 — 可维护性 / 长期健康

### B1. brain/nodes.py 单体拆分（REVISED 12.8）— 🟡 粗拆完成 @ 进行中
- **现状**：~~2361 行单文件~~ → 已拆 `brain/nodes/` 包（`__init__` re-export 保 `swarm.brain.nodes.X` 路径 100% 不变）。
- **已完成（拍板：先粗拆最大最常改的域 + 抽 shared，其余暂留）**：
  - `shared.py`：20 个无状态纯 helper + 常量
  - `dispatch.py`：dispatch / monitor 节点
  - `verify.py`：verify_l2 / verify_l3 + 失败态/巡检 helper
  - 🔑 mock 锚点零改：被 patch 的符号（`_get_brain_llm`/`_dispatch_to_worker`/`_get_project_path`/`_try_l2_*`/`_verify_l2_via_llm`）留 `__init__`，抽出节点内对其调用改 `nodes.X(...)` 模块限定 → `patch("swarm.brain.nodes.X")` 仍命中。
  - `__init__` 2361 → 1632 行；测试零改；723 passed。
- **暂留 `__init__`**：analyze / plan / validate_plan / confirm_plan / handle_failure / merge / deliver / revision / learn_success / learn_failure（后续需要时按同法继续抽）。
- **设计文档**：`docs/design/B1_nodes_split.md`。

### B2. 全子系统共进程 → 服务边界（REVISED 12.20）— 🟡 抽象地基完成 @ 进行中
- **现状**：API / Brain 编排 / Worker 执行 / 知识库 / 记忆 全在一个进程；核心耦合点 `_dispatch_to_worker` 同进程直跑 WorkerExecutor。
- **长期**：Worker 执行（重、可能 OOM）与 API（要稳）应进程/服务隔离。
- **核心判断（拍板）**：**B2 ≈ Docker 多容器交付**，落地与 Docker 合并（避免通信层返工）。
- **已完成（拍板：抽象先行，单机零变化）**：
  - `infra/worker_dispatcher.py`：`WorkerDispatcher` 接口 + `InProcessDispatcher`（默认，行为与拆分前逐字节一致）+ `get_worker_dispatcher()` 工厂（`SWARM_WORKER_DISPATCH_MODE` 切换点，queue 未实现时回退 inprocess）。
  - `_dispatch_to_worker` 改为走 dispatcher 接口；单机/当前部署零变化；5 单测覆盖。
- **待落地（与 Docker 合并）**：`QueueDispatcher`（PG 任务队列 SKIP LOCKED）+ 独立 Worker 容器 + Worker 写 PG 进度回传。
- **设计文档**：`docs/design/B2_service_boundaries.md`。延续 A1 留地基范式（Coordination/Leadership/状态外置 PG/配置 env 全就绪）。

### C. Docker 一键拉起（Swarm 自身可一键部署）— 🟢 文件就绪，待 CI 冒烟实跑
- **范围澄清（务必勿混淆）**：Docker 化的是 **Swarm 自身**（API/Brain编排/Worker子进程/知识库/记忆）打包成容器，`docker compose up` 一键拉起整个 Swarm 服务栈。**CubeSandbox 是独立的远程沙箱执行服务器，架构不动、不容器化、不进 compose**；Worker 经 `SWARM_SANDBOX_*` env 注入连接参数，仅作 SDK 客户端。
- **已交付**：
  - `Dockerfile`：多阶段（py3.12-slim 对齐 CI）、`pip install .`、非 root、`/api/health` healthcheck。
  - `docker-compose.yml`：`postgres`(pgvector/pg16) + `qdrant` + `swarm` 三服务；swarm `depends_on` PG/Qdrant `service_healthy`；service 名互联；startup 钩子幂等建全表（无需单独 init_db）；volumes 持久化。
  - `.env.docker.example` 模板 + `.dockerignore`（排除密钥/缓存/test/docs）。
  - `.github/workflows/docker.yml`：Docker Smoke —— 云端真实 `compose build + up`，验证 healthy + `/api/health` + 建表。
- **验证状态**：compose 结构静态校验全过；本机（macOS）无 Docker 运行时未能实跑，已加 CI 冒烟 workflow，**push 后 GitHub runner 真实拉起验证**（诚实标注，未自欺宣称本机拉起成功）。

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
