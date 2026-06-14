# DESIGN DOC: A1 — 单进程内存态 → 状态外置化

> 状态：**v1.0 已确认（2026-06-14）**，可进入 §3 渐进明细与批 1 实施
> 作者：CTO ｜ 日期：2026-06-14 ｜ 基线：v0.6.3 @ 798228a
> 关联：docs/ARCHITECTURE_ROADMAP.md A1（P0，其它架构项的前置）

---

## 0. 一句话目标

让 Swarm 能以**多副本**（多 uvicorn worker / 多容器）运行而不丢状态、不重复执行、不互相误杀沙箱——把三类进程内内存态外置到共享存储。

---

## 1. 现状复核（基于真实代码，非假设）

A1 涉及三类内存态，复核后**成熟度差异很大**，必须分别对待：

### 1.1 LangGraph checkpointer —— 🟡 基础设施已就位，生产路径未接通
- `compile_brain_graph(checkpointer=AsyncPostgresSaver|None)` 已支持传 PG saver（graph.py:476）。
- `compile_brain_graph_with_postgres()` 已实现（graph.py:530）。
- **但** runner/API 实际用的单例入口 `get_compiled_brain_graph()`（graph.py:515）调的是**不传 checkpointer 的 `compile_brain_graph()` → 回退 MemorySaver**。
- **结论**：不是从零做，是「接通已写好的一半」。核心工作 = 让单例入口走 PG saver + 处理 async 生命周期。

### 1.2 热沙箱池 SandboxPool —— 🔴 纯进程内内存态
- `_pool: dict[str, list[_PoolEntry]]` + `_borrowed` 计数 + `threading.Lock`（sandbox_pool.py:66-76），全在进程内。
- 多副本下：每个副本各管各的池，`max_total` 配额各算各的（实际总数 = N×max_total），孤儿归属混乱。
- **本轮已验证的基础**：CubeSandbox 支持 metadata 写入+回读+`SandboxQuery` 过滤（v0.6.x 实测），是「按实例标签管理沙箱」的技术基础。

### 1.3 后台调度器 —— 🔴 每副本各跑一份
- 4 个调度器在 startup 各起一个 asyncio task（app.py:683-686）：memory_decay / kb_update / consistency / task_scheduler。
- 多副本下：每个调度器跑 N 次 → 重复消费、重复 decay、重复全量重预处理。
- `_warn_if_multiprocess()` 已存在（app.py:739）——说明**当前是有意识地假设单进程**。

---

## 2. 已确认决议（2026-06-14 拍板）

| # | 疑问 | 决议 | 理由 |
|---|------|------|------|
| **Q1** | 协调存储 | **全 PG**，但**存储层抽象成可热拔插接口**（留地基，后续可换 Redis 无需改业务） | 良好的数据存储应可热拔插；当前 PG 够用，不引入新组件 |
| **Q2** | 沙箱池 | **优先实例隔离**（轻），但**留全局共享池的地基**（接口预留） | 解决误杀+配额失控核心痛点；全局池超出当前 ROI |
| **Q3** | 调度器 | **PG 选主**，封装成 `SchedulerLeadership` 抽象（**为服务化拆独立进程留地基**：拆出去的进程=永久 leader，抽象不变） | 见性能评估（下），无性能问题 |
| **Q4** | 节奏 | **分 3 批各自可回滚，但持续做完**（不止步批 1） | 小步可回滚 + 完整交付 |
| **Q5** | 交付形态 | **最终 Docker 项目交付**（多容器，优于裸服务器）；**当前编码阶段在本机直接跑** | 设计朝 Docker 收敛，验证当前在本机做 |

### 2.1 调度器选主性能评估（Q3 要求）

| 调度器 | 频率 | 负载 | 选主开销 |
|--------|------|------|---------|
| task_scheduler | 持续（准入队列，单机本就有界并发） | 中 | 仅 leader 跑，**消除 N 副本重复准入** |
| kb_update | 每 5s 轮询 PG 队列 | 轻 | advisory lock 检查微秒级，无压力 |
| consistency | 每日 04:00（全项目一致性+MR） | 重 | 每日一次，选主零压力 |
| memory_decay | 每日 | 轻 | 每日一次，零压力 |

**结论**：选主**无性能问题**。最频繁的 kb_update 也只是每 5s 一次 PG 会话级 advisory lock 检查（微秒级）。
选主反而**修复当前多副本会重复执行的浪费**（N 副本各跑一遍 consistency = N 倍全量预处理）。
**地基**：`SchedulerLeadership` 抽象让"仅 leader 执行"可平滑演进为 B2（调度器独立进程=永久 leader）。

### 2.2 "留地基"的具体含义（贯穿三批）

- **存储抽象**：定义 `CoordinationBackend` 接口（lease/lock/leader_election），PG 实现 `PgCoordinationBackend`；将来 Redis 实现 `RedisCoordinationBackend` 即插即用，业务代码不感知。
- **沙箱池抽象**：`SandboxPoolStrategy` 接口，当前 `InstanceIsolatedPool`，将来 `SharedPool` 可替换。
- **调度器抽象**：`SchedulerLeadership`，当前进程内选主，将来独立进程托管。
- **Docker 收敛**：所有外置状态走 PG（容器间共享），无本地文件依赖；配置全 env/db 驱动（已是现状）。

---

## 3. 渐进明细（已确认，可实施）

> 三批各自独立可上线/回滚，但**持续做完**（Q4）。每批含：地基抽象 → 实现 → 降级保障 → 验证。

### 批 1：checkpointer 接通 PG（地基：CoordinationBackend 雏形 + checkpointer 持久化）✅ 完成 @ v0.6.4
1. [x] `get_compiled_brain_graph()` 改为优先走 PG checkpointer（修复原 `compile_brain_graph_with_postgres` 的 async with 立即关连接 bug → `init_postgres_checkpointer` 持有连接到 app 生命周期）。
2. [x] **async 生命周期处理**：在 startup 钩子 `__aenter__`、shutdown `__aexit__`；runner 在同一 event loop 用 graph，无跨 loop hang。
3. [x] checkpointer 表迁移：startup 幂等 `setup()` + 并入 scripts/init_db.py 统一入口（实测建表成功）。
4. [x] **降级保障**：PG 不可用 → init 返回 False，get_compiled_brain_graph 回退 MemorySaver + warning（单测验证）。
5. [x] 验证：test_a1_pg_checkpointer.py 真 PG 跨"副本"(独立 checkpointer 连同一 PG) resume 通过；全量 692 passed。

### 批 2：调度器选主（地基：SchedulerLeadership 抽象）✅ 完成 @ v0.6.5
1. [x] `CoordinationBackend` 接口 + `PgCoordinationBackend`（pg_try_advisory_lock，专属长连接持锁，blake2b 稳定 key）。
2. [x] `SchedulerLeadership` + `run_as_leader_loop` + 进程级后端单例（init/close 对齐 app 生命周期）。
3. [x] startup `_run_schedulers_with_leadership`：leader 副本启动 4 调度器，非 leader 每 30s 重试抢主。
4. [x] **降级保障**：backend=None/PG 不可用 → try_become_leader 恒 True（本进程即 leader，单机不变）。
5. [x] 验证：test_a1_scheduler_leadership.py 真 PG 互斥+接管+降级通过；全量 694 passed。

### 批 3：沙箱池实例隔离（地基：SandboxPoolStrategy 抽象 + 复用本轮 metadata）✅ 完成 @ v0.7.0
1. [x] `get_instance_id()` 进程级稳定 ID（SWARM_INSTANCE_ID 可注入，否则随机 UUID）。
2. [x] `manager.create` 打 `metadata={"swarm_instance": <id>, swarm_project, swarm_task}`（复用本轮实测的 CubeSandbox metadata 能力；SDK 不支持时降级无标签）。
3. [x] `_fetch_sandbox_list_from_server` 回读 metadata；`_partition_sweep_targets` 按本实例过滤；`_sweep_startup_orphans` 只清本实例残留——**12.2 opt-in 开关升级为根治**（开关降级为"无标签沙箱是否清"）。
4. [x] `_partition_sweep_targets` 纯函数抽出（可单测）；SandboxPoolStrategy 全局池扩展点留待真需求。
5. [x] **降级保障**：metadata 不支持 → 无标签创建 + 开关控制；实例读取失败 → 保守默认。
6. [x] 验证：test_a1_sandbox_isolation.py(5) + test_sweep_orphans_optin_12_2.py(4，含"别副本绝不误杀"安全保证)；全量 701 passed。

---

## 3b. Docker 交付收敛（Q5，贯穿但不阻塞编码）

- **目标形态**：docker-compose / 多容器：`swarm-api`(可多副本) + `postgres`(pgvector) + `qdrant` + （CubeSandbox 为外部集群，经 dev_sidecar）。
- **A1 与 Docker 的关系**：A1 做完，多个 `swarm-api` 容器共享 PG 即可水平扩展——A1 是 Docker 多副本的前提。
- **本阶段**：编码/验证在本机（2 进程模拟多副本）；Docker 化作为 A1 完成后的独立交付项（单独 DESIGN DOC 或 ROADMAP 条目）。
- **设计约束**（现在就遵守，避免将来返工）：无本地文件状态依赖、配置全 env/db 驱动、所有外置状态走 PG。

---

## 4. 不做什么（范围边界）

- **不做**全局共享沙箱池（Q2 已定实例隔离；仅预留 `SandboxPoolStrategy` 扩展点）——超出当前 ROI。
- **不做**调度器拆独立服务（B2 范畴；本批仅留 `SchedulerLeadership` 地基，A2 服务化时拆）。
- **不碰** Worker 执行隔离（A2，单独 DESIGN DOC）。
- **不引入** Redis（Q1 已定全 PG；仅留 `CoordinationBackend` 接口，将来真需要再加 Redis 实现）。
- **不在本批做** Docker 化（Q5 已定为 A1 完成后的独立交付项；但本批遵守"无本地文件状态"约束避免返工）。

---

## 5. 风险与回滚

| 风险 | 缓解 |
|------|------|
| AsyncPostgresSaver event-loop 绑定 hang（记忆有先例） | lifespan 内初始化，不在 asyncio.run-per-call 处建 |
| PG checkpointer 性能（每节点写） | 先测吞吐；必要时调 checkpoint 粒度 |
| 选主脑裂 | advisory lock 是 PG 会话级，连接断自动释放，无脑裂 |
| 改动破坏单机/CI 开箱即用 | 每批保留降级路径 + 全量回归门 |

---

*v1.0 已确认。下一步：实施批 1（checkpointer 接通 PG）。每批完成后回归 + 提交，按 v0.6.x 节奏推进。*
