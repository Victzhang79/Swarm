# 🐝 Swarm — 蜂群 AI 编程智能体系统

> **本目录即项目根**（代码、配置、脚本、测试均在此）。从仓库克隆后请 `cd swarm` 再操作。

基于 **LangGraph** 的多智能体协作编程系统：**Brain 编排** → **难度路由** → **Worker 沙箱** → **知识检索** → **V2/V3 验证** → **记忆学习闭环**。

架构设计：[`docs/Swarm_System.html`](docs/Swarm_System.html) · 团队试用：[`docs/TEAM_TRIAL.md`](docs/TEAM_TRIAL.md)

---

## 目录结构

```
swarm/                          ← 项目根（当前目录）
├── README.md                   ← 本文档
├── setup.sh                    ← 一键安装 + 启动 API
├── pyproject.toml              ← pip install -e .
├── .env.example                ← 环境变量模板
├── api/                        ← FastAPI + Web UI (static/)
├── auth/                       ← RBAC · 默认 L1 profile
├── brain/                      ← LangGraph 15 节点状态机
├── worker/                     ← ReAct Agent · L1 pipeline · sandbox
├── knowledge/                  ← Layer A-D · 检索 · 增量调度
├── memory/                     ← L0-L6 · sliding_window · decay
├── infra/                      ← Redis 模块锁 / 任务队列（原 platform，避 stdlib 冲突）
├── project/                    ← PG store · preprocess · diff_apply
├── models/                     ← ModelRouter
├── cli/                        ← Click CLI
├── config/                     ← pydantic-settings
├── tools/                      ← Agent tools
├── workdir/                    ← greenfield 从零创建项目的默认根目录
├── scripts/                    ← 运维脚本
│   ├── init_db.py              ← 统一建表（schema 单一事实来源）
│   ├── start-services.sh       ← Qdrant + API
│   ├── restart-api.sh          ← 重载 API
│   ├── stop-api.sh
│   ├── run_milestone_check.sh
│   ├── benchmark_accept_rate.py
│   └── e2e-dotenv-flow.py
├── test/                       ← 全部测试与沙箱 sidecar
│   ├── test_*.py               ← 单元 / 集成测试
│   ├── swarm_bootstrap.py      ← import swarm 包（免污染 sys.path）
│   ├── run_all.sh              ← pytest 一键跑
│   ├── sandbox/                ← dev_sidecar.py（CubeSandbox 代理）
│   └── legacy/                 ← 历史 ad-hoc 脚本
└── docs/
    ├── Swarm_System.html
    └── TEAM_TRIAL.md
```

---

## 系统概览

```
┌─────────────────────────────────────────────────────────────────┐
│  交互层 — Web :8420 · REST/SSE · CLI (swarm)                    │
├─────────────────────────────────────────────────────────────────┤
│  Brain — ANALYZE→PLAN→VALIDATE→DISPATCH⇄MONITOR→MERGE         │
│          → verify_l2 (V2) → verify_l3 (V3) → DELIVER → LEARN   │
├─────────────────────────────────────────────────────────────────┤
│  Worker — ReAct · L1 流水线 · CubeSandbox                       │
├─────────────────────────────────────────────────────────────────┤
│  记忆 L0-L6 · 知识 L4 (A-D) · KB 入队增量 · Redis 模块锁(可选)   │
├─────────────────────────────────────────────────────────────────┤
│  PostgreSQL · Qdrant · LangSmith                                │
└─────────────────────────────────────────────────────────────────┘
```

### 命名澄清（`memory/layers.py`）

| 代号 | 含义 |
|------|------|
| Memory L0-L6 | 会话 / 画像 / 任务摘要 / **滑动窗口** / 知识库 / 错题 / 成功模式 |
| V1 / V2 / V3 | Worker L1 · `verify_l2` 集成 · `verify_l3` GitLab CI |

---

## 快速开始

### 前置

Python ≥3.11 · PostgreSQL 16 + pgvector · SiliconFlow API Key ·（推荐）Qdrant · CodeGraph · CubeSandbox

### 安装

```bash
cd swarm                    # 进入项目根
bash setup.sh
bash setup.sh --skip-pg --skip-env   # 已有 PG / .env
bash setup.sh --dev                  # + pytest 冒烟
```

> **建表**：`setup.sh` 在装完依赖后调用 `scripts/init_db.py` 统一建表。
> 所有表 DDL 由各业务模块（`project/store.py`、`memory/store.py`、`knowledge/*`、`auth/store.py`）定义，
> 应用启动钩子也调用相同的 `ensure_tables`，**单一事实来源、永不漂移**。
> 已有库可单独执行：`python scripts/init_db.py`。

访问 **http://localhost:8420**（默认 `admin` / `swarm`）

### 日常运维

```bash
bash scripts/start-services.sh
bash scripts/restart-api.sh      # 改代码或 .env 后
bash scripts/stop-api.sh
tail -f swarm.log
```

### 开发安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

`.env` 可从 `.env.example` 复制；日志与 PID 默认写在项目根。

---

## 当前能力摘要

| 域 | 要点 |
|----|------|
| Brain | 并行 dispatch（**依赖驱动 DAG**·批次容错）· 3-way merge · PlanValidator · shared_contract · **确定性失败升级阶梯**（retry→换模型→人工）|
| 验证 | Worker L1（语法+**lint**+单测+**LLM 自检**）· **harness 确定性闸门**（Brain 编写 build/test/verify，真跑命令覆盖 LLM 自报）· V2 gate 硬阻断 · V3 GitLab push/CI · integration_review |
| 知识 | hybrid 检索（**中文关键词+时间权重+共现过滤**）· approve/webhook 入队 · **按项目批量合并消费** · Layer C **规范自动提取** · consistency repair |
| 记忆 | L1 结构化 UI · L2 注入 analyze/plan · L3 滑动窗口 · PatternExtractor · **L5+L6 衰减** |
| 项目 | **导入现有目录 / greenfield 从零创建** · CodeGraph 预处理 · FileScope（writable/create/delete/allow_any）|
| 平台 | RBAC · `infra/` Redis 模块锁 · **任务级联取消（删项目→终止运行中任务+沙箱）** · **append-only 任务审计日志** · `scripts/init_db.py` 统一建表 |

---

## Web UI

| Tab | 功能 |
|-----|------|
| 任务 | Brain SSE · Plan/Diff 审阅 · apply-diff · cancel/retry |
| Worker | Phase 0 直跑 |
| 预处理 | scan/index/embed/analyze |
| 知识库 | Layer A/B · 检索 · Harness · 一致性 |
| 记忆 | L1 画像 · L5/L6 · 任务摘要 |
| 系统 | 健康 · 统计 · 学习趋势 · 组件状态(chip+悬浮详情) · 沙箱 |

> 通知已从系统 tab 迁移到右上角**铃铛**（持久化 `notifications` 表 + 未读绿点 + 浮窗逐条归档）。

---

## CLI

```bash
swarm submit "描述" -p <project-id> --watch
swarm worker-run "子任务" -p <pid> --watch
swarm task approve|revise|reject|cancel|retry <tid>
swarm profile show|set -p <pid>
swarm errors list -p <pid>
swarm patterns list -p <pid>
swarm check
```

---

## API 速查

```bash
# 登录
curl -X POST http://localhost:8420/api/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"swarm"}'

# 任务
curl -X POST http://localhost:8420/api/projects/<pid>/tasks \
  -H "Authorization: Bearer TOKEN" \
  -d '{"description":"..."}'

# 从零创建项目（greenfield，path 可留空自动建目录）
curl -X POST http://localhost:8420/api/projects \
  -H "Authorization: Bearer TOKEN" \
  -d '{"name":"sokoban","greenfield":true}'

# 任务审计日志（append-only，删除后仍可追溯）
curl 'http://localhost:8420/api/tasks/audit?project_id=<pid>' \
  -H "Authorization: Bearer TOKEN"

# 知识库
curl 'http://localhost:8420/api/projects/<pid>/knowledge/consistency?repair=true'
curl -X POST http://localhost:8420/api/projects/<pid>/knowledge/webhook/git \
  -d '{"commits":[...]}'
```

---

## 测试

> 当前 **432 passing**（不含需外部 CubeSandbox 的 `test_sandbox_integration.py`）。

```bash
# 推荐
bash test/run_all.sh
bash test/run_all.sh test/test_brain_phase3.py -v

# 或
python -m pytest test/ -q

# 单文件（内置 bootstrap）
python test/test_smoke.py

# 需外部服务
python test/test_knowledge_brain.py    # PostgreSQL
python test/test_sandbox_integration.py  # CubeSandbox

# E2E / 基准
python scripts/e2e-dotenv-flow.py --skip-preprocess
python scripts/benchmark_accept_rate.py --project-id <pid> --phase 1
```

| 测试 | 覆盖 |
|------|------|
| `test_smoke.py` | 模块导入 · graph 编译 |
| `test_brain_phase3.py` | dispatch/merge/V2/V3 |
| `test_p0_path.py` / `test_p1_p2_p3_path.py` | 关键路径 |
| `test_memory_architecture.py` | L0-L6 |
| `test_kb_scheduler.py` | KB 去重 · 入队 · **按项目批量合并** |
| `test_sliding_window.py` | L3 压缩 |
| `test_plan_validator.py` | 计划校验 |
| `test_l1_pipeline.py` | Worker L1 lint + LLM 自检 |
| `test_norms_extractor.py` | Layer C 规范自动提取 |
| `test_stats_api.py` | 统计 + 通知端点（list/unread/archive） |
| `test_scheduler.py` | 准入调度 · TaskQueue 优先级 · 模块锁 · 项目软限制 |
| `test_l3_gitlab.py` | V3 GitLab 配置 · pipeline 触发轮询 · MR 创建（mock httpx） |
| `test_runner.py` | 任务状态判断(orphaned/can_retry) · SSE 队列 · 通知钩子 |
| `test_updater.py` | 增量更新 dedupe/merge · AST 符号抽取 · handle_event 分发 |
| `test_executor.py` | scope 收集 · 路径归一化 · L1 自报解析 · **本地模式 diff 快照** |
| `test_cn_keywords.py` | 中文 2-gram 关键词抽取 · 时间衰减加权 |
| `test_cascade_cancel.py` | 删项目级联取消 · 幽灵任务终止 · 不误伤其他项目 |
| `test_harness_l1.py` | harness 语言推断 · L1 优先用 harness.test_command · verify_commands 硬阻断 |
| `test_file_operations.py` | FileScope create/delete/allow_any · greenfield 开放式需求放行 |

---

## 环境变量

见 [`.env.example`](.env.example)。常用：

| 变量 | 说明 |
|------|------|
| `SWARM_DB_POSTGRES_URI` | PostgreSQL |
| `SWARM_MODEL_SILICONFLOW_API_KEY` | 默认云端接入点（SiliconFlow）API Key |
| `SWARM_MODEL_PROVIDERS` | 多接入点（JSON list）：每个 `{id,type,base_url,api_key,kind}`，支持任意多个云端/本地端点；空则由 siliconflow+local 两个扁平字段合成（向后兼容）。设置 tab「🔌 模型接入点」可视化增删，含 OpenRouter/DeepSeek/MiniMax/Kimi/GLM/Qwen/xAI/OpenAI 等 10 个预置 |
| `SWARM_MODEL_MODEL_PROVIDERS` | 模型→接入点显式映射（JSON dict），覆盖"按模型名猜"的启发式 |
| `SWARM_NOTIFY_CHANNELS` | 外部通知渠道（JSON list）：`{id,type,webhook_url,enabled,events}`，系统每产生一条通知即推送到 enabled 且事件匹配的渠道（飞书/钉钉/企业微信/Slack/通用）。设置 tab「📢 通知渠道」可视化配置 + 测试 |
| `SWARM_OBS_CLICKHOUSE_*` | 可观测数据源（OpenLIT/ClickHouse），LLM/embed/rerank 调用 trace；「📊 可观测」面板展示 p95/慢调用/错误率，不可达时降级 |
| `SWARM_CONTEXT_MAX_TOKENS` | Memory L3 预算 |
| `SWARM_GITLAB_*` | V3 验证 / MR |
| `SWARM_REDIS_ENABLED` | 模块锁 |
| `SWARM_RBAC_ENABLED` | 多用户 |
| `SWARM_SANDBOX_POOL_ENABLED` | 沙箱热池（**默认开**，预热复用省去每任务冷启动；设置 tab 可一键开关，池状态见系统 tab + `GET /api/sandbox/pool`）|
| `SWARM_WORKER_L1_LINT` / `_LINT_GATE` / `_SELF_REVIEW` | Worker L1 确定性闸门开关（默认全开硬阻断）|
| `SWARM_WORKER_COMMAND_WHITELIST` | 全局命令白名单（harness.extra_whitelist 在其上追加）|
| `SWARM_LANGSMITH_TRACING` | LangSmith 追踪开关（L1 确定性证据回写为结构化 feedback）|
| `SWARM_LOG_LEVEL` | 日志级别 DEBUG/INFO/WARNING/ERROR（默认 INFO） |
| `SWARM_LOG_FILE` | 日志文件路径（默认 swarm.log，空串=仅控制台） |
| `SWARM_LOG_JSON` | true=结构化 JSON 行日志（便于聚合） |
| `SWARM_LOG_MAX_BYTES` / `SWARM_LOG_BACKUP_COUNT` | 轮转大小/保留数（默认 20MB×5） |

### 模型接入点（多云端 + 本地）

接入点（provider）是一等公民：每个模型显式声明归属哪个接入点，**不再靠"模型名含 / 就是云端"的脆弱启发式**。

- **多接入点**：云端可配任意多个（SiliconFlow / OpenRouter / DeepSeek / MiniMax / Kimi / GLM / Qwen / xAI / OpenAI…），本地推理一个或多个。设置 tab「🔌 模型接入点」每行下拉切换预置，预置只需填 API Key，本地可改 Base URL，自定义端点展开全字段。
- **预置目录** `GET /api/model-providers/catalog`：10 个常用云端的权威 `base_url`（参照 Hermes-Agent）。
- **向后兼容零迁移**：`SWARM_MODEL_PROVIDERS` 为空时，由 `_effective_providers()` 从老的 `siliconflow_*`+`local_*` 扁平字段合成两个接入点；保存内置 id 时同步回写老字段，`/api/models` 等老读取点不受影响。

### 外部通知渠道

系统每产生一条通知（任务创建/完成/失败/待审）即推送到外部渠道 —— **单一注入点**：`store.create_notification` 写库后触发 hook → `api/notify.dispatch_notification` 遍历 enabled 渠道按事件过滤推送，覆盖所有通知来源不会漏。

- 支持飞书 / 钉钉 / 企业微信 / Slack incoming webhook + 通用 HTTP POST。
- 设置 tab「📢 通知渠道」配置：每渠道选类型、填 webhook、勾选订阅事件（空=全部）、即时「测试」。
- 渠道列表预留 `user_id`（当前空=全局；多用户时按用户投递，无需改结构）。
- 旧的单 webhook（`SWARM_NOTIFY_WEBHOOK_URL` + `SWARM_NOTIFY_FORMAT`）保留向后兼容。

### 日志系统

统一入口 `swarm/logging_config.py`，API / CLI / 脚本 / cron 共用：

- **轮转文件**：`RotatingFileHandler`（默认 20MB×5），修复 `swarm.log` 无限增长
- **task 上下文贯穿**：`bind_task(task_id)` 用 contextvar 跨协程传播，并发任务日志带 `[task=xxxxxxxx sub=st-N]` 前缀，可按任务追踪
- **可选 JSON 结构化**：`SWARM_LOG_JSON=true` 输出每行 JSON（ts/level/logger/msg/task_id），便于 ELK/Loki
- **配置驱动**：级别、文件、轮转、控制台开关全走 `AppConfig.log_*` / 环境变量

---

## 故障排除

| 现象 | 处理 |
|------|------|
| 任务 409 | 先完成预处理 |
| DELIVERING 暂停 | approve/revise/reject 或 `SWARM_AUTO_ACCEPT=true` |
| DISPATCHING 卡住 | cancel/retry 或 `DELETE ?force=true` |
| KB 未更新 | 看日志 `[KBScheduler]` · `consistency?repair=true` |
| pytest 导入错误 | 在项目根 `pip install -e .` |
| `platform` 冲突 | 已 rename 为 `infra/`，勿恢复 `platform/` 目录名 |

---

## 已知差距与 Roadmap

> 对照设计文档（`docs/Swarm_System.html` V2）走读全部 26K 行代码后的诚实评估。
> 核心链路（提交→Brain 编排→Worker 沙箱→审核→Learn→KB 增量）端到端可用，
> 经多轮强化后整体完成度约 **95%**。设计文档列出的差距已全部补齐。

### 最新一轮 — 生命周期 / harness 工程 / greenfield / 并行（实测驱动）

> 真实运行中暴露并修复的问题，每项均有任务日志 + 沙箱日志佐证（非单测推断）。

**生命周期 / 资源**

- ✅ **删项目不取消运行中任务 → 幽灵任务烧 GPU** → 删项目前级联 `cancel_project_tasks`：取消 asyncio 句柄 + 释放沙箱；`cancel_task` 即使 DB 记录已删仍清理内存句柄，杜绝陷入 replan 死循环（`brain/runner.py`、`api/routers/project.py`）。实测：删运行中项目→「级联取消 1 个运行中任务」→25s 零新活动→孤儿沙箱 0。
- ✅ **warmup 死代码每次 dispatch 泄漏孤儿沙箱** → 移除失效的临时 `SandboxPool.warmup`（实例即弃、远端沙箱永不回收）。

**可追溯性 / 防误删**

- ✅ **任务硬删后无任何痕迹** → 新增 `task_audit_log`（append-only，永不随删除清除）：create/delete/级联删都留痕，`GET /api/tasks/audit` 可查（`project/store.py`、`api/routers/task.py`）。
- ✅ **删除确认太弱** → WebUI 删项目二次确认，明确告知级联取消 + 硬删 N 个任务不可恢复（`api/static/js/core/project.js`）。

**harness 验证工程（确定性闸门，杜绝"自报合格"）**

- ✅ **Worker 只被告知"run_compile/run_tests"但无项目命令、白名单固定** → 新增 `TaskHarness`（language/build/test/verify_commands/extra_whitelist）：Brain 在 PLAN 时为每个子任务编写 harness（prompt 强制，必填）；`_infer_harness` 按语言兜底（python/node/go/rust/java）（`types.py`、`brain/nodes.py`、`brain/prompts.py`）。
- ✅ **小模型拿不到验证依据 / 命令白名单拒绝** → worker prompt 注入 harness 段「必须实跑命令」；`run_command` 白名单 = 全局 + 本任务 `harness.extra_whitelist`（`worker/prompts.py`、`tools/build_tools.py`）。
- ✅ **L1 凭 LLM 自报通过** → L1 优先用 `harness.test_command`，新增 L1.3.5 真跑 `verify_commands` 硬阻断；空 diff 但有 harness 也跑确定性验证；确定性证据回写 LangSmith 结构化 feedback（`worker/l1_pipeline.py`、`tracing.py`）。
- ✅ **trivial 快速路径绕过 L1（纯字符串判 "fail"）** → 端到端实测发现：`Sorry, need more steps`（未完成）也被判通过 high。改为 pull-back 后跑 `_deterministic_l1_gate`，实测从「无决策来源」变为「来源: deterministic」（`worker/executor.py`）。
- ✅ **环境漂移误判**（实测发现）→ L1 闸门本地跑命令时：`_python_bin` 优先 `sys.executable`（带 pytest 的 venv，非系统 python3）；裸 `python`→可用解释器归一化。

**greenfield 从零创建 + 并行**

- ✅ **只能导入现有目录** → `POST /api/projects` 支持 `greenfield=true`（path 可空，自动建 `workdir/<name>`）；WebUI 加「导入现有 / 从零创建」单选；`FileScope.allow_any` 让开放式需求可自由建文件（`api/routers/project.py`、`api/static/js/core/project.js`、`types.py`）。实测：「写个推箱子游戏」→生成可运行 229 行代码。
- ✅ **派发偏串行**（LLM 把独立子任务拆进各自 group）→ `get_dispatch_batch` 改为**依赖驱动**：派发所有 `depends_on` 已满足的子任务并行执行，`parallel_groups` 降为软提示（`types.py`）。

### 最新一轮 — RuoYi 混编 E2E 实战（沙箱池 + 真实编译验证打通）

> 用真实企业级多模块 Java 项目（RuoYi，6 模块）+ 开启沙箱热池跑端到端，
> 每个 bug 均有任务日志 + 沙箱日志 + mvn 编译结果佐证（非单测推断）。
> 成果：trivial 改动「代码生成→沙箱编译→diff 收集→merge→DONE」全链路真通，
> `mvn -pl <mod> -am compile exit=0` + L1 确定性通过 + 干净 diff + 单次无重试循环。

**沙箱 / 编译验证（5 语言生产级的真实落地）**

- ✅ **L1 确定性闸门只编 py/js，不验 Java/Go/Rust** → 新增 L1.2.1 build 闸门，
  在沙箱里真跑 `harness.build_command`（mvn/go build/cargo）；新增 `_run_l1_command`
  沙箱优先执行器（工具链在沙箱，不在本机）（`worker/l1_pipeline.py`）。
- ✅ **502 Bad Gateway（shell 命令走 Jupyter，语言镜像无 kernel）** → build/clean/
  健康探针/pull-back 文件枚举/webui 读文件与列目录全切原生 shell 端点（`commands.run`）
  （`tools/build_tools.py`、`worker/sandbox.py`、`worker/executor.py`）。
- ✅ **mvn 找不到 POM / 多模块 reactor 秒挂** → 同步构建清单（pom/gradle/go.mod/
  Cargo.toml 等）+ 多模块按改动模块限定 `mvn -pl <mod> -am`（`worker/executor.py`、
  `worker/l1_pipeline.py`）。
- ✅ **cannot find symbol 秒挂** → 编译型语言（JVM 系）同步【改动所在模块的完整源码树】，
  解决「精准 scope 同步」与「整模块编译」的根本矛盾（`worker/executor.py`）。
- ✅ **构建/测试闸门工程文件缺失误判失败**（如 Java 项目里的纯前端 JS 触发 npm）→
  `_build_cmd_applicable` 按工具链工程文件存在性优雅跳过（`worker/l1_pipeline.py`）。

**diff 收集 / 闸门正确性（杜绝假通）**

- ✅ **「DONE 但 merged_diff 为空」假通 + 重试死循环** → diff 基线改用 git HEAD 提交版，
  防本地工作副本被前序运行 pull-back 污染（`worker/executor.py`）。
- ✅ **CRLF/LF 行尾不一致 → 整文件 churn 垃圾 diff（44KB 全删全增，真实改动被淹没）** →
  diff 比较前归一化行尾（`worker/executor.py`）。
- ✅ **空 diff 却因「原代码能编译」误判 PASS** → 空 diff + 期望有产出（writable/
  create_files 非空）直接判失败，触发重试/换模型，杜绝「没干活」假 DONE（`worker/executor.py`）。

**小模型可用性 / 上下文**

- ✅ **小模型反复「need more steps」** → trivial 路径 recursion_limit 12→30，撞上限优雅
  交确定性闸门按真实文件状态裁决（`worker/executor.py`）。
- ✅ **196k 上下文顶穿（read_file 无界返回整文件 + run_command 无界输出）** → 工具输出硬
  上限（read 450 行/32KB，命令输出 compress 4KB），防 ReAct message 历史爆炸（`tools/`）。
- ✅ **embedding 端点硬编码 localhost:3000** → 改用配置 `SWARM_MODEL_LOCAL_BASE_URL`（`project/preprocess.py`）。

> 已知外部变量：本地模型（ai.bit:3000 网关）偶发流式 stall（120s 无 chunk），属远端
> 推理服务可靠性问题，现有 fallback 容错可恢复；待办见 [`TECH_DEBT.md`](TECH_DEBT.md)。

### 本轮已修复

**P1 — 正确性/鲁棒性**

- ✅ **Brain 子任务重试无上限** → `state.py` 新增 `subtask_retry_counts`，`handle_failure` 实现确定性升级阶梯：`retry(≤max_retries) → retry_alternate(换模型) → escalate(人工)`，LLM 决策不再能突破硬上限（`brain/nodes.py`）
- ✅ **换模型降级缺确定性递进** → 同上，由每子任务重试计数器强制档位，不再依赖 LLM 单次决策
- ✅ **revision 清空全部结果** → 改为保留已完成子任务产出，仅派发新增 rev-* 子任务（`brain/nodes.py`）
- ✅ **dispatch 批次首个失败即丢弃兄弟结果** → 改为收集整批 outcome 后统一返回（`brain/nodes.py`）
- ✅ **Worker L1 缺 lint + LLM 自检** → 补齐 L1.2.5 lint（ruff/eslint，error 级才失败，优雅降级）+ L1.4 LLM 自检（结构化 JSON，不硬阻断），环境变量 `SWARM_WORKER_L1_LINT` / `SWARM_WORKER_L1_SELF_REVIEW` 开关（`worker/l1_pipeline.py`）
- ✅ **L6 成功模式衰减未实现** → `mem_successes` 加 `decay_weight` 列，实现 `decay_l6`（衰减因子 0.95，比 L5 的 0.9 更温和；reuse_count 高的衰减更慢），日衰减同时跑 L5+L6（`memory/decay.py`）
- ✅ **L5 全项目衰减失效** → `project_id=None` 时改为全表 batch SQL UPDATE，不再用匹配不到的 `'__all__'`
- ✅ **embedding 零向量静默失效** → 占位函数首次调用醒目告警（不刷屏），写入时检测零向量并打 `embedding_placeholder=true` 标记；L5 写入统一为 pgvector 格式（原 Jsonb 格式不被索引）

**P2 — 设计增强**

- ✅ **增量更新无批量合并** → PG 队列按 project_id 分组合并去重（同文件保留最后状态），每项目一次处理，实现设计的"5s 窗口批量"效果（保留 PG 轮询，未引入 Redis）（`knowledge/updater.py`）
- ✅ **检索三项增强** → 中文关键词抽取（2-gram + 中文停用词）、时间权重（文件越新得分越高）、共现交叉过滤（Layer D 共现提权），均优雅降级（`knowledge/retriever.py`）
- ✅ **Layer C 规范自动提取** → 新增 `knowledge/norms_extractor.py`，从 `.editorconfig`/`pyproject.toml`/`.ruff.toml`/`setup.cfg`/`.eslintrc`/`.prettierrc`/`pom.xml` 提取规范写入 `tag='auto'`，预处理 SCAN 阶段后自动调用

**基础设施**

- ✅ **schema 双源漂移** → setup.sh 不再硬编码 DDL，统一调用 `scripts/init_db.py`（各模块 DDL 单一事实来源）
- ✅ **KB 配置前缀不一致** → `SWARM_KNOWLEDGE_*` 全部对齐为 `SWARM_KB_*`
- ✅ **setup.sh 冗余依赖块** → 删除（pyproject.toml 已声明全部依赖）

**第二轮 — 设计文档剩余差距补齐**

- ✅ **增量更新正则抽取符号** → Python 改用 stdlib **ast** 精确解析（嵌套类/async/装饰器/docstring/准确行号），语法错误回退正则；其他语言保留正则（`knowledge/updater.py`）
- ✅ **合并缺 rebase 重生成** → 3-way merge 与硬冲突之间新增中间档：选一方为 base 保留，另一方子任务标记 `rebase_subtask_ids` 重跑（不计入重试次数），`after_merge` 路由到 dispatch（`brain/merge_engine.py`、`brain/nodes.py`、`brain/graph.py`）
- ✅ **WebSocket 未实现** → 新增 `WS /ws/tasks/{task_id}` 与 SSE 并存，复用同一事件队列（`api/app.py`）
- ✅ **外部通知** → `api/notify.py` 多渠道分发：`store.create_notification` 单一注入点触发 hook，推送飞书/钉钉/企业微信/Slack/通用 webhook；设置 tab 可视化配置 + 测试（`SWARM_NOTIFY_CHANNELS`，预留多用户 user_id），未配置静默跳过
- ✅ **任务队列无优先级** → `TaskQueue` 支持 urgent>normal>background（Redis 三 List + 内存 fallback 同步），向后兼容；新增 `check_project_limit()` 软限制（`SWARM_MAX_ACTIVE_PROJECTS`，默认 10）（`infra/redis_client.py`）
- ✅ **embedding 不可用无降级路径** → Layer B 失败时 Layer A 独立成功，文件暂存 `kb_pending_embeddings` 重试队列，`retry_pending_embeddings()` 在服务恢复后补处理（`knowledge/updater.py`）

**第三轮 — 全项目梳理（代码走读 + 测试审查后修复）**

> 走读全部 26K 行代码（brain/worker/knowledge/memory/project）+ 审查 30 个测试文件后修复的真实 bug。

- ✅ **本地执行模式产出空 diff** → `WorkerExecutor._pre/_post_sync_contents` 未在 `__init__` 初始化，无沙箱降级时 `_get_git_diff` 永远返回"(无变更)"。新增 `_snapshot_scope_local()`，本地模式直接快照 writable 文件前后内容（`worker/executor.py`）
- ✅ **L5 批量衰减与逐条不一致** → `decay_l5_batch_sql` 原用平坦 `decay_weight*factor`，忽略 occurrence_boost；改为 CASE + `POWER(factor, 1/occurrence_count)`，与逐条 `decay_l5` 公式对齐（`memory/decay.py`）
- ✅ **Qdrant point ID 跨进程碰撞** → 原用 Python 内置 `hash()`（PYTHONHASHSEED 随机化），重复预处理同符号生成不同 ID、旧向量残留；改用稳定 `blake2b` 哈希（`project/preprocess.py`）
- ✅ **检索有副作用（违反 CQRS）** → `retrieve_for_brain` 每次检索都对 top-5 错题/成功自增 occurrence/reuse，反复检索人为推高权重扭曲衰减；移除检索期自增，复用计数应在模式实际采纳时单独触发（`knowledge/retriever.py`）
- ✅ **embedding 重试队列无自动调度** → `retry_pending_embeddings()` 接入 KBScheduler 轮询（每 60s 一次），并加 `retry_count<10` 上限避免永久失败无限空转（`knowledge/scheduler.py`、`knowledge/updater.py`）
- ✅ **学习趋势永远"未知"** → `_get_learning_effectiveness` 原只看 mem_mistakes，无错题→unknown；改为综合 mem_successes，新增 `learning`（无错题有成功=健康）/`regressing` 趋势（`project/store.py`、前端 `memory.js`）

> **测试审查 + 补测**：209→291 passing（+82）。新增 6 个真实单测文件覆盖此前零直接单测的核心模块：`test_scheduler.py`（准入调度/队列优先级/锁）、`test_l3_gitlab.py`（V3 GitLab，mock httpx）、`test_runner.py`（任务生命周期状态判断）、`test_updater.py`（增量更新 dedupe/AST/handle_event 分发）、`test_executor.py`（scope/路径/本地模式 diff 快照）、`test_cn_keywords.py`（中文关键词，从项目根 print 脚本归类为正式测试）。补测过程发现并修复一个真实 bug：orphaned 的人工审核态任务被错误允许重跑（`runner.can_retry_task` 调整审核态拦截顺序）。**假绿治理**：`test_worker_api` 的 git 静默跳过改为 `pytest.skip`；`test_sandbox_integration` 加沙箱可达性门控（不可达时整体 skip 而非报错，`SWARM_RUN_SANDBOX_IT=1` 强制运行）。

---

## License

MIT
