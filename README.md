# 🐝 Swarm

> 不是又一个 AI 编程助手 —— 而是一套**对交付结果负责**的多智能体工程系统。

大模型很会写代码，但**很会自信地交付错的东西**。在"满地都是编程智能体"的今天，真正的瓶颈早已不是"让 AI 写出代码"，而是**怎么让一群自主的 AI 在没有人盯着时，把活干对、干完、并且每一步都可追溯**。

Swarm 解决的就是这件事：把一个需求交给一套**有分工、有验证、有记忆**的 AI 智能体团队——

- **Brain** 理解需求、做技术设计、把活拆成子任务；
- **Worker** 在隔离沙箱里实际写代码、跑构建、自我验证；
- **确定性闸门** 在 LLM 自评之前，先用编译/测试/lint 这类**不靠模型嘴说**的硬标准卡一道；
- **记忆系统** 把每次审核反馈沉淀下来，让系统越用越懂这个项目。

> **和 Cursor / Copilot / Claude Code 的区别**：它们是"坐在你旁边帮你写"的副驾，由你逐行把关；Swarm 是"接过整个需求、自己拆解执行、跑完验证再交还给你"的工程班子。前者优化的是**单人手速**，Swarm 优化的是**无人值守下的交付可信度**——这恰恰是智能体越自主、越不可或缺的那一层。

**为什么"流程"在 agent 时代反而更重要？** 模型能力越强、越敢自己做主，越需要一层不依赖它自我感觉的客观约束：谁拆的任务、谁执行的、过没过确定性闸门、哪条记忆影响了决策——全程状态机驱动、可追溯、可回放。这不是上个时代的瀑布流程，这是让自主智能体**可被信任**的工程问责制。

---

## ✨ 核心特性

- **多智能体编排**：Brain（编排）→ 难度路由 → Worker（执行）→ 验证 → 记忆学习，全程状态机驱动（LangGraph）。
- **隔离沙箱执行**：Worker 在 CubeSandbox（E2B 兼容）中写代码、跑构建/测试，支持**项目级定制沙箱**（按项目真实环境构建专属镜像，依赖缓存命中、构建零下载）。
- **混合模型路由**：子任务按难度（trivial / medium / complex / multimodal）路由到不同模型，本地扛简单量、云端啃难任务。可接 SiliconFlow / OpenAI / OpenRouter / DeepSeek / 智谱 / 本地推理等任意 OpenAI 兼容接入点。
- **模型能力分级（可选）**：按 Brain 主模型能力自动收紧/放宽编排约束——强模型少澄清/少打回/少二次拆分（降延迟），弱模型多兜底。默认关闭（行为零变化），Web UI 一键启用 + A/B。
- **自动并行编排**：剥离 LLM 误加的"假依赖"，让真正独立的子任务并行执行（依赖 DAG 驱动，非保守串行），merge 冲突检测兜底。
- **代码知识库**：基于符号表 + 向量检索（embedding + rerank，可配云端或自建），为每个任务精准注入相关代码上下文；Worker 可按需即时检索（just-in-time），不止预灌。
- **确定性验证闸门**：Worker 产物先过确定性 L1 闸门（编译/测试/lint），再走 LLM 审查，最后人工 accept —— **不把"模型说它对了"当成"它真的对了"**：修复轮用真实编译/lint 证据驱动；返工重规划时清空旧完成态、防"提前宣告完成"（premature victory）。
- **记忆学习闭环**：每次审核反馈沉淀为分层记忆（L0–L6），影响后续编排与生成。
- **小模型友好的上下文治理**：Worker 在有限上下文窗口的小模型上也能稳定干活——ReAct 历史按 token 预算自动裁剪、文件按需局部读取、子任务 scope 精确收窄到最小文件集，避免"大模型做脑、小模型做手"时小模型上下文被撑爆。
- **配置式、开箱即用**：模型、沙箱、Embedding/Rerank 接入点、检索调优均可在 Web UI 配置，敏感 Key 加密存储，保存即生效。

---

## 📦 环境依赖

| 依赖 | 版本 | 必需 | 说明 |
|---|---|---|---|
| Python | ≥ 3.11 | ✅ | 推荐 3.12 |
| PostgreSQL | 16 + [pgvector](https://github.com/pgvector/pgvector) | ✅ | 任务/项目/记忆/向量元数据存储 |
| [Qdrant](https://qdrant.tech/) | ≥ 1.13 | ✅ | 代码向量库（检索）。setup.sh 自动下载本地二进制或用 Docker |
| LLM 接入点 | OpenAI 兼容 API | ✅ | 至少配一个（云端 key 或本地推理服务） |
| [CodeGraph CLI](https://github.com/colbymchenry/codegraph) | latest | ⬜ | 预处理时构建符号表/依赖图；缺失则跳过该阶段，不影响主链路 |
| CubeSandbox / E2B | — | ⬜ | 隔离沙箱执行；留空则 Worker 本地执行 |
| Embedding / Rerank 服务 | OpenAI 兼容 | ⬜ | 可走云端（SiliconFlow 等）或自建；缺失时回退内置 fastembed |
| Redis | ≥ 6 | ⬜ | 模块锁 / 任务队列；默认关闭 |
| [Docker](https://docs.docker.com/) + Compose v2 | — | ⬜ | 用「方式一 Docker 一键拉起」时需要；裸机部署不需要 |

**操作系统**：macOS（Apple Silicon）/ Ubuntu 22.04+ / Debian / RHEL 系（setup.sh 自动适配 brew / apt / dnf）。

---

## 🚀 快速开始

### 方式一：Docker 一键拉起（最快，推荐试用）

整套 Swarm 服务栈（API + PostgreSQL/pgvector + Qdrant）一条命令拉起，无需手动装依赖：

```bash
git clone https://github.com/Victzhang79/Swarm.git
cd Swarm/swarm                   # 项目根在内层 swarm/ 目录
cp .env.docker.example .env      # 按需填 LLM Key / CubeSandbox 地址等（不填也能起，登录后在 WebUI 配）
docker compose up -d --build     # 拉起 postgres + qdrant + swarm 三容器
```

启动后访问 **http://localhost:8420**（默认登录 `admin` / `swarm`，首次登录强制改密）。
启动钩子会自动建表，无需手动初始化。

> **注意**：Docker 化的是 **Swarm 自身**；**CubeSandbox（远程沙箱执行服务器）是独立服务**，不在 compose 内。Worker 通过 `SWARM_SANDBOX_*` 环境变量连它（在 `.env` 填），留空则 Worker 退回本地执行。

### 方式二：一键安装脚本（裸机部署）

```bash
git clone https://github.com/Victzhang79/Swarm.git
cd Swarm/swarm          # 项目根在内层 swarm/ 目录
bash setup.sh           # 9 步全自动：系统依赖 → pgvector → PG → venv → Python 依赖 → 建表 → CodeGraph → .env → Qdrant → 启动
```

`setup.sh` 会交互式引导你填入 LLM API Key 等配置，全部完成后服务启动在 **http://localhost:8420**。

常用选项：

```bash
bash setup.sh --skip-pg          # 跳过 PostgreSQL 安装（已有 PG）
bash setup.sh --skip-codegraph   # 跳过 CodeGraph CLI
bash setup.sh --skip-env         # 跳过 .env 交互式配置
bash setup.sh --dev              # 额外装开发依赖 + 跑冒烟测试
bash setup.sh --help             # 查看全部选项
```

### 方式三：手动安装

```bash
# 1. 准备 PostgreSQL 16 + pgvector，创建数据库 swarm
createdb swarm && psql -d swarm -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 2. Python 虚拟环境 + 依赖
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .                 # 运行时依赖（pyproject.toml）
pip install -e ".[dev]"          # 含 pytest / ruff（可选）

# 3. 配置 .env（参考 .env.example）
cp .env.example .env             # 然后填入 API Key / DB URI 等

# 4. 建表
python scripts/init_db.py

# 5. 启动依赖服务 + API
bash scripts/start-services.sh   # Qdrant + Swarm API
```

### 验证运行

```bash
curl http://localhost:8420/api/health      # 健康检查
open http://localhost:8420                  # Web UI（默认登录 admin / swarm）
```

---

## 🧭 日常运维

| 脚本 | 作用 |
|---|---|
| `docker compose up -d` | Docker 方式：一键拉起全栈（首选试用） |
| `docker compose down` | Docker 方式：停止全栈（加 `-v` 清数据卷） |
| `bash setup.sh` | 裸机一键安装 + 启动（首次部署） |
| `bash scripts/start-services.sh` | 启动 Qdrant + API（已装好后日常启动） |
| `bash scripts/restart-api.sh` | 重载 API（代码 / .env 变更后） |
| `bash scripts/stop-api.sh` | 停止 API |
| `bash test/run_all.sh` | 运行全部测试 |

CLI：

```bash
swarm --help                     # CLI 帮助
swarm submit -p <project_id> --watch   # 提交任务并跟踪
```

---

## 🏗️ 架构概览

```
┌──────────────────────────────────────────────────────────┐
│  交互层   Web UI (:8420) · REST / SSE · CLI (swarm)       │
├──────────────────────────────────────────────────────────┤
│  Brain    理解需求 → 技术设计 → 拆解子任务 → 派发/监控/合并 │
│           （LangGraph 状态机，含交互式澄清子图）            │
├──────────────────────────────────────────────────────────┤
│  路由     按难度 trivial/medium/complex/multimodal 选模型  │
├──────────────────────────────────────────────────────────┤
│  Worker   ReAct Agent 在沙箱写代码 → L1 确定性闸门 → 验证  │
├──────────────────────────────────────────────────────────┤
│  知识库   符号表 + 向量检索（embed + rerank）注入上下文     │
│  记忆     L0–L6 分层记忆，审核反馈驱动学习闭环              │
├──────────────────────────────────────────────────────────┤
│  存储     PostgreSQL+pgvector · Qdrant · (可选 Redis)      │
└──────────────────────────────────────────────────────────┘
```

| 模块 | 目录 | 职责 |
|---|---|---|
| API + Web UI | `api/` | FastAPI 服务 + 静态前端 |
| Brain | `brain/` | LangGraph 编排状态机 |
| Worker | `worker/` | ReAct Agent · L1 验证 · 沙箱构建 |
| 知识库 | `knowledge/` | 检索 · embedding · rerank · 增量调度 |
| 记忆 | `memory/` | L0–L6 分层记忆 · 衰减 |
| 项目 | `project/` | PG 存储 · 预处理 · diff 应用 · 沙箱推断 |
| 模型 | `models/` | 多接入点路由 |
| 配置 | `config/` | pydantic-settings · 密钥加密存储 |
| CLI | `cli/` | Click 命令行 |

---

## ⚙️ 服务与端口

启动后涉及以下进程：

| 服务 | 端口 | 进程 | 必需 |
|---|---|---|---|
| Swarm API + Web UI | 8420 | uvicorn | ✅ |
| Qdrant | 6333 / 6334 | qdrant | ✅ |
| PostgreSQL | 5432 | postgres | ✅ |
| Redis | 6379 | redis | ⬜（默认关闭） |

外部依赖（按需）：LLM 接入点、Embedding/Rerank 服务、CubeSandbox 沙箱宿主。

---

## 💻 资源占用（参考）

最小可跑（单机开发）：

- **CPU**：2–4 核
- **内存**：4–8 GB（Qdrant + PostgreSQL + Python 服务本体约 1–2 GB；其余为向量库与并发 Worker 余量）
- **磁盘**：约 2–5 GB（Python 依赖含 torch/fastembed 较大；向量库与 .codegraph 随项目规模增长）
- **GPU**：不需要（模型推理在外部 LLM 接入点/服务，本机不跑大模型）

> Embedding/Rerank、LLM 推理均通过外部接入点完成，Swarm 本体是轻量编排服务。若使用本地 fastembed 兜底嵌入，首次会下载 bge-m3 模型（约数百 MB）。

---

## 🔧 配置

配置通过 `.env`（`SWARM_*` 前缀）与 Web UI「设置」面板双轨管理：

- **模型接入点**：可配多个 OpenAI 兼容接入点（云端 / 本地），Brain 与 Worker 各层路由自由选择模型。
- **Embedding / Rerank**：可配云端成熟服务（SiliconFlow / OpenAI / Cohere）或自建服务，敏感 Key 加密存储。
- **沙箱**：CubeSandbox 接入信息；支持项目级定制沙箱模板。
- **敏感信息**：API Key 等通过 `secret_store` 加密存储，不以明文落 `.env`。

完整变量见 [`.env.example`](.env.example)。Web UI 中修改的配置保存即生效（热重载）。

---

## ❓ 常见问题

**Q：预处理时 index 阶段被跳过？**
A：未安装 CodeGraph CLI。不影响 Brain 主链路；如需符号表检索，运行 `curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh`。

**Q：预处理跳过了向量嵌入？**
A：Qdrant 未启动。检查 `curl http://localhost:6333/collections`，或重跑 `bash scripts/start-services.sh`。

**Q：Web UI 模型下拉显示「配置 API Key」选不了模型？**
A：对应接入点未配 Key 或不可达。在「设置 → 模型接入点」填入 Key 并保存，点「刷新模型列表」。

**Q：Worker 没有沙箱，代码在哪执行？**
A：未配 CubeSandbox 时 Worker 本地执行。生产建议配置隔离沙箱。

**Q：端口 8420 被占用 / 改端口？**
A：`export SWARM_PORT=<port>` 后重启，或先 `bash scripts/stop-api.sh`。

**Q：数据库连不上 / 建表失败？**
A：确认 PostgreSQL 16 已启动、`swarm` 库存在、pgvector 扩展已启用，`.env` 中 `SWARM_DB_POSTGRES_URI` 正确，然后 `python scripts/init_db.py`。

---

## 🧪 测试

```bash
bash test/run_all.sh                                    # 全部测试
.venv/bin/python -m pytest test/ -q                     # 等价命令
.venv/bin/ruff check . --select E9,F63,F7,F82           # 关键 lint（CI 同款）
```

CI 在全新空 PostgreSQL（pgvector）+ Python 3.12 环境下运行 lint 与全量测试。

---

## 📄 License

[MIT](LICENSE)
