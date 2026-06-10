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
├── brain/                      ← LangGraph 17 节点状态机
├── worker/                     ← ReAct Agent · L1 pipeline · sandbox
├── knowledge/                  ← Layer A-D · 检索 · 增量调度
├── memory/                     ← L0-L6 · sliding_window · decay
├── infra/                      ← Redis 模块锁 / 任务队列（原 platform，避 stdlib 冲突）
├── project/                    ← PG store · preprocess · diff_apply
├── models/                     ← ModelRouter
├── cli/                        ← Click CLI
├── config/                     ← pydantic-settings
├── tools/                      ← Agent tools
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
| Brain | 并行 dispatch（批次容错）· 3-way merge · PlanValidator · shared_contract · **确定性失败升级阶梯**（retry→换模型→人工）|
| 验证 | Worker L1（语法+**lint**+单测+**LLM 自检**）· V2 gate 硬阻断 · V3 GitLab push/CI · integration_review |
| 知识 | hybrid 检索（**中文关键词+时间权重+共现过滤**）· approve/webhook 入队 · **按项目批量合并消费** · Layer C **规范自动提取** · consistency repair |
| 记忆 | L1 结构化 UI · L2 注入 analyze/plan · L3 滑动窗口 · PatternExtractor · **L5+L6 衰减** |
| 平台 | RBAC · `infra/` Redis 模块锁 · MR 历史日 sync · `scripts/init_db.py` 统一建表 |

---

## Web UI

| Tab | 功能 |
|-----|------|
| 任务 | Brain SSE · Plan/Diff 审阅 · apply-diff · cancel/retry |
| Worker | Phase 0 直跑 |
| 预处理 | scan/index/embed/analyze |
| 知识库 | Layer A/B · 检索 · Harness · 一致性 |
| 记忆 | L1 画像 · L5/L6 · 任务摘要 |
| 系统 | 健康 · 统计 · 通知 · 沙箱 |

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
  -H 'Authorization: Bearer <token>' \
  -d '{"description":"..."}'

# 知识库
curl 'http://localhost:8420/api/projects/<pid>/knowledge/consistency?repair=true'
curl -X POST http://localhost:8420/api/projects/<pid>/knowledge/webhook/git \
  -d '{"commits":[...]}'
```

---

## 测试

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

---

## 环境变量

见 [`.env.example`](.env.example)。常用：

| 变量 | 说明 |
|------|------|
| `SWARM_DB_POSTGRES_URI` | PostgreSQL |
| `SWARM_MODEL_SILICONFLOW_API_KEY` | 云端模型 |
| `SWARM_CONTEXT_MAX_TOKENS` | Memory L3 预算 |
| `SWARM_GITLAB_*` | V3 验证 / MR |
| `SWARM_REDIS_ENABLED` | 模块锁 |
| `SWARM_RBAC_ENABLED` | 多用户 |

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

> 对照设计文档（`docs/Swarm_System.html` V2）走读全部 22.8K 行代码后的诚实评估。
> 核心链路（提交→Brain 编排→Worker 沙箱→审核→Learn→KB 增量）端到端可用，
> 经两轮强化后整体完成度约 **95%**。设计文档列出的差距已全部补齐。

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
- ✅ **外部通知未接** → 新增 `api/notify.py`，支持飞书/Slack/通用 webhook（`SWARM_NOTIFY_WEBHOOK_URL` + `SWARM_NOTIFY_FORMAT`），未配置静默跳过，已接入 approve/revise/reject
- ✅ **任务队列无优先级** → `TaskQueue` 支持 urgent>normal>background（Redis 三 List + 内存 fallback 同步），向后兼容；新增 `check_project_limit()` 软限制（`SWARM_MAX_ACTIVE_PROJECTS`，默认 10）（`infra/redis_client.py`）
- ✅ **embedding 不可用无降级路径** → Layer B 失败时 Layer A 独立成功，文件暂存 `kb_pending_embeddings` 重试队列，`retry_pending_embeddings()` 在服务恢复后补处理（`knowledge/updater.py`）

---

## License

MIT
