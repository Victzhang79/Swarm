# B2 设计文档：全子系统共进程 → 服务边界

状态：**待拍板** · 版本 v0.1 · 2026-06-14
关联：ROADMAP P1 可维护性 · A1 状态外置化（已留全部地基）· Docker 多容器交付

---

## 1. 问题与现状（实测）

**当前**：API / Brain 编排 / Worker 执行 / 知识库 / 记忆，**全在一个 uvicorn 进程**。

**核心耦合点（实测）**：`brain/nodes.py::_dispatch_to_worker`（L1245）：
```python
from swarm.worker.executor import WorkerExecutor
executor = WorkerExecutor(subtask=subtask, ...)
output = await executor.run()   # ← in-process await，同进程内直接跑代码执行
```
Brain 编排进程里**直接 new 了 WorkerExecutor 并 await 执行**。Worker 是计算重活（拉沙箱、跑 build/test、可能 OOM），却和"要稳"的 API 同进程 —— 一个 Worker 把进程拖垮，API 跟着挂。

**痛点排序**：
1. **隔离性**：Worker OOM/卡死 → 拖垮 API（最实际的生产风险）
2. **独立伸缩**：Worker 是瓶颈，应能独立多副本扩容，API 不必跟着扩
3. **故障域**：Worker 崩溃不该影响编排状态（A1 已把状态外置 PG，崩溃可恢复）

---

## 2. A1 已铺好的地基（B2 的前提全部就绪）

| 地基 | A1 交付 | 对 B2 的意义 |
|------|---------|-------------|
| 状态全外置 PG | checkpointer + 业务状态全 PG | 进程间共享状态，Worker 进程崩溃可恢复 |
| `CoordinationBackend` | PG advisory lock（可换 Redis） | 跨进程协调/锁/选主原语就绪 |
| `SchedulerLeadership` | 进程内选主，"拆出去=永久 leader" | 调度器拆独立进程的抽象已就位 |
| 配置 env/db 驱动 | 无硬编码、无本地文件依赖 | 多容器各自起、共享配置 |
| 沙箱实例隔离 | metadata 打 instance/project/task 标签 | 多 Worker 副本不互相误杀沙箱 |

**结论**：A1 之后，B2 的数据/协调/配置层全部就绪，**只差"进程边界"这一刀**。

---

## 3. 🔑 核心工程判断：B2 ≈ Docker 多容器交付，应合并而非分别做

这是我要直接跟你讲清楚的范式判断：

**"把 Worker 拆成独立进程/服务"（B2）和"Docker 多容器交付"（最终形态）本质是同一件事的两面**：
- B2 拆进程边界 = 定义"哪些是独立部署单元"
- Docker 多容器 = 把这些独立单元各自打成容器

如果先做 B2（在裸进程层面拆 Worker 独立进程 + 进程间通信），再做 Docker（把它们容器化），**进程间通信层会被推翻重写一次**（裸进程的 IPC 方式 ≠ 容器网络下的方式）。**这是返工浪费。**

**正确做法**：B2 的"进程边界设计"直接朝 Docker 多容器形态收敛 —— 拆分时就用容器友好的跨进程机制（任务队列 / HTTP RPC），一步到位。

---

## 4. 候选架构（朝 Docker 多容器收敛）

### 4.1 拆分单元（建议）
```
┌─────────────┐   任务队列(PG/Redis)    ┌──────────────┐
│  API 容器    │ ──────────────────────> │ Worker 容器   │ (可 N 副本)
│ (FastAPI +   │ <────────────────────── │ (WorkerExec   │
│  Brain 编排) │   结果/状态回写 PG       │  + 沙箱调用)  │
└─────────────┘                          └──────────────┘
       │                                         │
       └──────────────┬──────────────────────────┘
                      ▼
              PostgreSQL (状态/checkpointer/队列)
              [+ Qdrant 向量 / 沙箱服务器(已独立)]
```

- **API + Brain 编排**：留一个容器（编排是 I/O 密集、轻），要稳。
- **Worker 执行**：独立容器，可多副本。`_dispatch_to_worker` 从 "in-process await" 改为 "投递任务到队列 + 等结果"。
- **通信机制候选**：
  - **(a) PG 任务队列**（`SELECT ... FOR UPDATE SKIP LOCKED` 拉活）：零新组件，复用现有 PG，A1 的 CoordinationBackend 同源。**我倾向这个**（最小依赖，符合"不引入新组件"原则）。
  - (b) Redis 队列 / RQ / Celery：成熟但引入新组件。
  - (c) HTTP RPC（Worker 暴露 /execute）：简单但要自己做重试/背压/发现。

### 4.2 不拆的部分
- 知识库 / 记忆：读多写少、轻，**留 API 容器**（拆它们 ROI 低）。
- 沙箱服务器：**已经是独立服务器**（CubeSandbox 远程），不在本次范围。

---

## 5. 风险与成本

| 项 | 评估 |
|----|------|
| 工作量 | **大**（B1 之上最大的一块）。`_dispatch_to_worker` 改造 + 队列层 + Worker 容器入口 + 端到端回归 |
| 风险 | 中高。但 A1 已把状态外置（崩溃可恢复）、协调原语就绪，风险被 A1 大幅降低 |
| 回归面 | dispatch/monitor/handle_failure 链路 + SSE 进度回传（Worker 在别的进程，进度怎么流回 API）|
| 可回滚 | 通信层抽象成接口（`WorkerDispatcher`：in-process 实现 vs queue 实现），开关切换，单机仍可 in-process |

---

## 6. 待确认疑问（请拍板）

| # | 疑问 | 选项 / 我的建议 |
|---|------|----------------|
| **Q1** | **B2 是否现在做？** 它是 ROADMAP 最重的一块，且强依赖"要拆成 Docker"这个最终目标。 | A. 现在做（独立于 Docker）；B. **与 Docker 多容器交付合并一起做**（我强烈倾向 B —— 见 §3，分开做会返工重写通信层）；C. 推迟，先只做 B1 |
| **Q2** | 若做，**通信机制**？ | (a) **PG 任务队列**（SKIP LOCKED，零新组件，我倾向）；(b) Redis/Celery；(c) HTTP RPC |
| **Q3** | **Worker → API 的进度回传**（当前 SSE 从同进程读；拆开后 Worker 进度怎么流回前端）？ | (a) Worker 写 PG，API 轮询/SSE 读 PG（与 A1 状态外置一致，我倾向）；(b) Worker 直连推送 |
| **Q4** | **拆分范围**：只拆 Worker，还是连 Brain 编排也独立？ | 我倾向**只拆 Worker**（编排轻、与 API 同容器；Worker 重、独立可扩）——最小必要拆分 |
| **Q5** | **抽象先行**：先引入 `WorkerDispatcher` 接口（in-process 实现保持现状 + queue 实现），让单机零变化、多容器可切换？ | 我强烈倾向**是** —— 留地基、可热拔插、单机开发不受影响（延续 A1 的"留地基"范式）|

---

## 7. 我的总体建议（CTO 视角）

1. **B1 先做**（低风险、独立、纯收益），按 B1 doc 的"粗拆 + 测试零改"方案。
2. **B2 与 Docker 多容器交付合并**作为一个大里程碑做 —— 因为它们是同一件事，分开做返工。先做 **Q5 的 `WorkerDispatcher` 抽象**（单机零变化），再在 Docker 化时落地 queue 实现。
3. 顺序：**B1（现在）→ WorkerDispatcher 抽象（B2 地基，可现在）→ Docker 多容器（B2 落地 + 最终交付，一个大里程碑）**。

这样每一步都不返工，符合你"留地基/可热拔插/朝 Docker 收敛"的一贯范式。
