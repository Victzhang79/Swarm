#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Swarm 一键拉起脚本 — 兼容 macOS (Apple Silicon) + Ubuntu 22.04+
#  用法: bash setup.sh [--skip-pg] [--skip-codegraph] [--skip-env] [--dev]
#
#  当前进度 (2026-06): Phase 0–5 主链路 ✅ · 记忆 L0-L6 · KB 入队调度 · V2/V3 双 gate
#  架构设计: docs/Swarm_System.html · 详见 README.md
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ─── 颜色 ───
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# ─── 参数解析 ───
SKIP_PG=false; SKIP_CODEGRAPH=false; DEV_MODE=false; SKIP_ENV=false
for arg in "$@"; do
    case "$arg" in
        --skip-pg)        SKIP_PG=true ;;
        --skip-codegraph) SKIP_CODEGRAPH=true ;;
        --skip-env)       SKIP_ENV=true ;;
        --dev)            DEV_MODE=true ;;
        --help|-h)
            echo "用法: bash setup.sh [选项]"
            echo "  --skip-pg         跳过 PostgreSQL 安装配置"
            echo "  --skip-codegraph  跳过 CodeGraph CLI 安装（预处理 index 阶段将跳过）"
            echo "  --skip-env        跳过 .env 交互式配置"
            echo "  --dev             安装开发依赖并运行 Phase0-5 冒烟测试套件"
            echo "  --help            显示帮助"
            echo ""
            echo "架构文档: docs/Swarm_System.html"
            echo "日常运维:"
            echo "  bash scripts/start-services.sh   # Qdrant + API"
            echo "  bash scripts/restart-api.sh      # 重载 API（代码 / .env 变更）"
            echo "  bash scripts/stop-api.sh         # 停止 API"
            echo "  python scripts/benchmark_accept_rate.py --help  # Phase 0/1 验收基准"
            exit 0 ;;
    esac
done

# ─── 检测系统 ───
OS="$(uname -s)"
ARCH="$(uname -m)"
info "系统: $OS $ARCH"

if [[ "$OS" == "Darwin" ]]; then
    PKG_MGR="brew"
    PG_PKG="postgresql@16"
    PG_BIN="/opt/homebrew/opt/postgresql@16/bin"
    PG_DATA="/opt/homebrew/var/postgresql@16"
elif [[ "$OS" == "Linux" ]]; then
    # 检测 Ubuntu / Debian
    if command -v apt-get &>/dev/null; then
        PKG_MGR="apt"
        PG_PKG="postgresql-16"
        PG_BIN="/usr/lib/postgresql/16/bin"
        PG_DATA="/var/lib/postgresql/16/main"
    elif command -v dnf &>/dev/null; then
        PKG_MGR="dnf"
        PG_PKG="postgresql16-server"
        PG_BIN="/usr/pgsql-16/bin"
        PG_DATA="/var/lib/pgsql/16/data"
    else
        warn "未检测到 apt/dnf，尝试继续..."
        PKG_MGR="unknown"
    fi
else
    fail "不支持的操作系统: $OS"
fi

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

# ═══════════════════════════════════════════════════════════════
#  Step 1: 系统依赖
# ═══════════════════════════════════════════════════════════════
info "━━━ Step 1: 系统依赖 ━━━"

if [[ "$PKG_MGR" == "brew" ]]; then
    if ! command -v brew &>/dev/null; then
        info "安装 Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    info "brew install $PG_PKG ..."
    brew install "$PG_PKG" 2>/dev/null || true

elif [[ "$PKG_MGR" == "apt" ]]; then
    info "apt: 安装 PostgreSQL 16 及构建工具..."
    sudo apt-get update -qq
    # PostgreSQL 官方源
    sudo apt-get install -y -qq wget curl gnupg2 lsb-release build-essential \
        pkg-config libpq-dev 2>/dev/null || true
    # 添加 PG APT 源 (如果尚未添加)
    if ! apt-cache policy | grep -q "apt.postgresql.org"; then
        sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
        curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg 2>/dev/null || true
        sudo apt-get update -qq
    fi
    sudo apt-get install -y -qq "$PG_PKG" "postgresql-contrib-16" "postgresql-server-dev-16" 2>/dev/null || true
fi

# ═══════════════════════════════════════════════════════════════
#  Step 2: pgvector 扩展
# ═══════════════════════════════════════════════════════════════
info "━━━ Step 2: pgvector 扩展 ━━━"

# 检查是否已安装
PGVECTOR_INSTALLED=false
if [[ -n "${PG_BIN:-}" ]] && "$PG_BIN/pg_config" --version &>/dev/null; then
    EXT_DIR="$("$PG_BIN/pg_config" --sharedir)/extension"
    if [[ -f "$EXT_DIR/vector.control" ]]; then
        ok "pgvector 已安装"
        PGVECTOR_INSTALLED=true
    fi
fi

if [[ "$PGVECTOR_INSTALLED" == "false" ]]; then
    info "从源码编译 pgvector v0.8.2 ..."
    PGCONFIG="${PG_BIN}/pg_config"

    if [[ ! -x "$PGCONFIG" ]]; then
        # 尝试找 pg_config
        PGCONFIG="$(command -v pg_config 2>/dev/null || true)"
    fi

    if [[ -n "$PGCONFIG" && -x "$PGCONFIG" ]]; then
        TMPDIR="$(mktemp -d)"
        trap 'rm -rf "$TMPDIR"' EXIT
        cd "$TMPDIR"
        curl -fsSL https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.2.tar.gz -o pgvector.tar.gz
        tar xzf pgvector.tar.gz
        cd pgvector-0.8.2
        make -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)" PG_CONFIG="$PGCONFIG"
        sudo make install PG_CONFIG="$PGCONFIG" 2>/dev/null || make install PG_CONFIG="$PGCONFIG"
        ok "pgvector v0.8.2 编译安装完成"
        cd "$PROJECT_ROOT"
    else
        warn "找不到 pg_config，跳过 pgvector 编译。请手动安装。"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 3: PostgreSQL 启动 & 数据库初始化
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_PG" == "false" ]]; then
    info "━━━ Step 3: PostgreSQL 启动 & 初始化 ━━━"

    # macOS: brew services
    if [[ "$PKG_MGR" == "brew" ]]; then
        brew services start "$PG_PKG" 2>/dev/null || true
        sleep 2
    # Linux: systemctl 或手动
    elif [[ "$PKG_MGR" == "apt" ]]; then
        sudo systemctl enable postgresql 2>/dev/null || true
        sudo systemctl start postgresql 2>/dev/null || true
    fi

    # 创建 swarm 数据库（如果不存在）
    CURRENT_USER="$(whoami)"
    DB_EXISTS="$(psql -U "$CURRENT_USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='swarm'" 2>/dev/null || echo "")"
    if [[ "$DB_EXISTS" != "1" ]]; then
        info "创建 swarm 数据库..."
        if [[ "$PKG_MGR" == "apt" ]]; then
            sudo -u postgres psql -c "CREATE DATABASE swarm;" 2>/dev/null || true
            sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE swarm TO $CURRENT_USER;" 2>/dev/null || true
        else
            createdb swarm 2>/dev/null || true
        fi
        ok "数据库 swarm 已创建"
    else
        ok "数据库 swarm 已存在"
    fi

    # 启用 pgvector 扩展（建表脚本也会再次确保，幂等）
    psql -d swarm -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null && ok "pgvector 扩展已启用" || warn "pgvector 扩展启用失败（如已启用可忽略）"

    info "数据库已就绪，数据表将在 Step 5（Python 依赖安装后）由 scripts/init_db.py 统一创建"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 4: Python 环境
# ═══════════════════════════════════════════════════════════════
info "━━━ Step 4: Python 虚拟环境 ━━━"

PYTHON_CMD=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    if [[ "$PKG_MGR" == "brew" ]]; then
        info "brew install python@3.12 ..."
        brew install python@3.12 2>/dev/null || true
        PYTHON_CMD="python3.12"
    elif [[ "$PKG_MGR" == "apt" ]]; then
        info "apt install python3.12 python3.12-venv ..."
        sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
        sudo apt-get update -qq
        sudo apt-get install -y -qq python3.12 python3.12-venv python3.12-dev 2>/dev/null || true
        PYTHON_CMD="python3.12"
    fi
fi

if ! command -v "$PYTHON_CMD" &>/dev/null; then
    fail "找不到 Python >= 3.11。请手动安装。"
fi
ok "Python: $($PYTHON_CMD --version)"

# 创建 venv
VENV_DIR="$PROJECT_ROOT/.venv"
if [[ ! -f "$VENV_DIR/bin/python" && -f "$PROJECT_ROOT/../.venv/bin/python" ]]; then
    VENV_DIR="$PROJECT_ROOT/../.venv"
    ok "使用上级目录虚拟环境: $VENV_DIR"
fi
if [[ ! -f "$VENV_DIR/bin/python" ]]; then
    info "创建虚拟环境..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

# 安装 pip 工具
if ! "$VENV_DIR/bin/python" -c "import pip" &>/dev/null; then
    info "安装 pip..."
    "$VENV_DIR/bin/python" -m ensurepip --upgrade 2>/dev/null || curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python"
fi

ok "虚拟环境就绪: $VENV_DIR"

# ═══════════════════════════════════════════════════════════════
#  Step 5: Python 依赖
# ═══════════════════════════════════════════════════════════════
info "━━━ Step 5: Python 依赖安装 ━━━"

# 优先用 uv（快 10-100x）
PIP_CMD=""
if command -v uv &>/dev/null; then
    PIP_CMD="uv pip"
elif "$VENV_DIR/bin/python" -c "import uv" &>/dev/null; then
    PIP_CMD="$VENV_DIR/bin/python -m uv pip"
fi

if [[ -z "$PIP_CMD" ]]; then
    # 安装 uv
    info "安装 uv 包管理器..."
    curl -fsSL https://astral.sh/uv/install.sh | sh 2>/dev/null || true
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uv &>/dev/null; then
        PIP_CMD="uv pip"
    else
        PIP_CMD="$VENV_DIR/bin/pip"
    fi
fi

info "使用 $PIP_CMD 安装依赖..."

# 核心依赖（pyproject.toml 已声明全部运行时依赖）
$PIP_CMD install -e "$PROJECT_ROOT" 2>&1 | tail -3

if [[ "$DEV_MODE" == "true" ]]; then
    $PIP_CMD install pytest pytest-asyncio ruff 2>&1 | tail -3
fi

ok "Python 依赖安装完成"

# ═══════════════════════════════════════════════════════════════
#  Step 5.4: 数据表初始化（schema 单一事实来源 = scripts/init_db.py）
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_PG" == "false" ]]; then
    info "━━━ Step 5.4: 数据表初始化 ━━━"
    if "$VENV_DIR/bin/python" "$PROJECT_ROOT/scripts/init_db.py"; then
        ok "数据表就绪（由各业务模块 DDL 统一创建）"
    else
        warn "建表失败，请检查 PostgreSQL 连接与 .env 配置"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 5.5: 集成测试（知识检索 + learn 落库）
# ═══════════════════════════════════════════════════════════════
if [[ "$DEV_MODE" == "true" ]]; then
    info "━━━ Step 5.5: 集成测试 ━━━"
    if "$VENV_DIR/bin/python" test/test_knowledge_brain.py \
        && "$VENV_DIR/bin/python" test/test_smoke.py \
        && "$VENV_DIR/bin/python" test/test_file_tools_sandbox.py \
        && "$VENV_DIR/bin/python" test/test_worker_api.py \
        && "$VENV_DIR/bin/python" test/test_task_lifecycle.py \
        && "$VENV_DIR/bin/python" test/test_knowledge_hooks.py \
        && "$VENV_DIR/bin/python" test/test_learn_chain.py \
        && "$VENV_DIR/bin/python" test/test_knowledge_api.py \
        && "$VENV_DIR/bin/python" test/test_memory_api.py \
        && "$VENV_DIR/bin/python" test/test_brain_phase3.py \
        && "$VENV_DIR/bin/python" test/test_stats_api.py \
        && "$VENV_DIR/bin/python" test/test_benchmark.py \
        && "$VENV_DIR/bin/python" test/test_kb_scheduler.py \
        && "$VENV_DIR/bin/python" test/test_sliding_window.py \
        && "$VENV_DIR/bin/python" test/test_memory_architecture.py \
        && "$VENV_DIR/bin/python" test/test_p0_path.py \
        && "$VENV_DIR/bin/python" test/test_plan_validator.py; then
        ok "集成测试通过 (Phase0-5 + 记忆/KB/P0 路径)"
    else
        warn "部分集成测试失败，请检查 PostgreSQL / 依赖"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 6: CodeGraph CLI（colbymchenry/codegraph — 项目预处理用）
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_CODEGRAPH" == "false" ]]; then
    info "━━━ Step 6: CodeGraph CLI ━━━"
    export PATH="$HOME/.local/bin:$PATH"

    install_codegraph() {
        info "安装 CodeGraph CLI (@colbymchenry/codegraph)..."
        if curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh; then
            return 0
        fi
        warn "官方 install.sh 失败，尝试 npm 全局安装..."
        if command -v npm &>/dev/null; then
            npm i -g @colbymchenry/codegraph && return 0
        fi
        return 1
    }

    if ! command -v codegraph &>/dev/null; then
        install_codegraph || warn "CodeGraph 安装失败，预处理 index 阶段将跳过（不影响 Brain 主链路）"
    fi

    export PATH="$HOME/.local/bin:$PATH"
    if command -v codegraph &>/dev/null; then
        CG_VER="$(codegraph --version 2>/dev/null || echo 'unknown')"
        ok "CodeGraph: $CG_VER"
        info "预处理会在项目目录生成 .codegraph/codegraph.db（符号表 + 依赖图）"
    else
        warn "CodeGraph 未在 PATH 中（不影响核心功能；可稍后手动安装）"
        info "手动安装: curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 7: .env 配置
# ═══════════════════════════════════════════════════════════════
if [[ "$SKIP_ENV" == "false" ]]; then
    info "━━━ Step 7: .env 配置 ━━━"

    ENV_FILE="$PROJECT_ROOT/.env"

    if [[ ! -f "$ENV_FILE" ]]; then
        # 交互式生成 .env
        echo ""
        info "需要配置以下参数（留空使用默认值）:"
        echo ""

        read -rp "SiliconFlow API Key [必填]: " SF_KEY
        read -rp "SiliconFlow Base URL [https://api.siliconflow.cn/v1]: " SF_URL
        SF_URL="${SF_URL:-https://api.siliconflow.cn/v1}"

        read -rp "本地模型 API Key [留空跳过]: " LOCAL_KEY
        read -rp "本地模型 Base URL [http://ai.bit:3000/api]: " LOCAL_URL
        LOCAL_URL="${LOCAL_URL:-http://ai.bit:3000/api}"

        read -rp "PostgreSQL URI [postgresql://localhost:5432/swarm]: " PG_URI
        PG_URI="${PG_URI:-postgresql://localhost:5432/swarm}"

        read -rp "CubeSandbox API URL [留空=本地执行]: " SBX_API
        read -rp "CubeSandbox Proxy Base [留空跳过]: " SBX_PROXY
        read -rp "CubeSandbox Template ID [留空跳过]: " SBX_TPL

        read -rp "LangSmith API Key [留空跳过]: " LS_KEY
        read -rp "LangSmith Project [swarm-dev]: " LS_PROJ
        LS_PROJ="${LS_PROJ:-swarm-dev}"

        cat > "$ENV_FILE" << ENVEOF
# ──────────────────────────────────────────────
# Swarm 环境配置 — 由 setup.sh 自动生成
# ──────────────────────────────────────────────

# 数据库
SWARM_DB_POSTGRES_URI=${PG_URI}
SWARM_DB_REDIS_URI=redis://localhost:6379/0
SWARM_DB_QDRANT_URL=http://localhost:6333

# 模型 — SiliconFlow
SWARM_MODEL_SILICONFLOW_BASE_URL=${SF_URL}
SWARM_MODEL_SILICONFLOW_API_KEY=${SF_KEY}
SWARM_MODEL_BRAIN_PRIMARY=Pro/zai-org/GLM-5.1
SWARM_MODEL_BRAIN_FALLBACK=moonshotai/Kimi-K2.6

# 模型 — 本地
SWARM_MODEL_LOCAL_BASE_URL=${LOCAL_URL}
SWARM_MODEL_LOCAL_API_KEY=${LOCAL_KEY}
SWARM_MODEL_WORKER_PRIMARY=MiniMax-M2.7-Pro

# Brain 编排参数
SWARM_MODEL_BRAIN_TEMPERATURE=0.3
SWARM_MODEL_TIMEOUT_SECONDS=120
SWARM_MODEL_MAX_RETRIES=2

# Worker 参数
SWARM_MODEL_WORKER_TEMPERATURE=0.2
SWARM_MODEL_WORKER_LOCAL=qwen3:27b

# 模型路由（本地模型名必须与服务器实际列表完全一致）
SWARM_MODEL_ROUTING_TRIVIAL=Qwen3.6-27B-Saka-NVFP4
SWARM_MODEL_ROUTING_TRIVIAL_FALLBACK=Step-3.7-Flash
SWARM_MODEL_ROUTING_MEDIUM=MiniMax-M2.7-Pro
SWARM_MODEL_ROUTING_MEDIUM_FALLBACK=Qwen3.5-122B-A10B-NVFP4
SWARM_MODEL_ROUTING_COMPLEX=Pro/zai-org/GLM-5.1
SWARM_MODEL_ROUTING_COMPLEX_FALLBACK=moonshotai/Kimi-K2.6
SWARM_MODEL_ROUTING_MULTIMODAL=Qwen3.6-27B-Saka-NVFP4-multimodal
SWARM_MODEL_ROUTING_MULTIMODAL_FALLBACK=MiniMax-M2.7-Pro

# Worker
SWARM_WORKER_MAX_CONCURRENT=4
SWARM_WORKER_MAX_EXECUTION_TIME=600
SWARM_WORKER_MAX_ITERATIONS=50
SWARM_WORKER_MAX_FIX_ROUNDS=3

# 任务自动化（/api/demo 默认 auto_accept；任务主链路默认等待人工审核）
# SWARM_AUTO_ACCEPT=true
# SWARM_API_URL=http://127.0.0.1:8420

# 知识库
SWARM_KB_EMBEDDING_MODEL=BAAI/bge-m3
SWARM_KB_RERANKER_MODEL=BAAI/bge-reranker-v2-m3

# 记忆 L3 滑动窗口（Brain 编排上下文压缩，非 verify_l3）
# SWARM_CONTEXT_MAX_TOKENS=80000
# SWARM_CONTEXT_RESERVE_TOKENS=16000

# Redis 平台（可选 — 模块锁 / 任务队列）
# SWARM_REDIS_ENABLED=false

# RBAC（默认开启；Web 登录 admin / swarm）
# SWARM_RBAC_ENABLED=true
# SWARM_BOOTSTRAP_ADMIN_PASSWORD=swarm

# GitLab V3 验证 + accept 后 MR（可选）
# SWARM_GITLAB_URL=
# SWARM_GITLAB_TOKEN=
# SWARM_GITLAB_PROJECT_ID=
# SWARM_GITLAB_PUSH_ENABLED=false
# SWARM_GITLAB_MR_ON_ACCEPT=false
ENVEOF

        # 沙箱（可选）
        if [[ -n "$SBX_API" ]]; then
            cat >> "$ENV_FILE" << ENVEOF

# CubeSandbox（E2B SDK 兼容）
SWARM_SANDBOX_API_URL=${SBX_API}
SWARM_SANDBOX_PROXY_BASE=${SBX_PROXY}
SWARM_SANDBOX_DEFAULT_TEMPLATE=${SBX_TPL}
SWARM_SANDBOX_USE_FOR_WORKER=true
SWARM_SANDBOX_SANDBOX_FIRST=true
ENVEOF
        fi

        # LangSmith（可选）
        if [[ -n "$LS_KEY" ]]; then
            cat >> "$ENV_FILE" << ENVEOF

# LangSmith 追踪
SWARM_LANGSMITH_TRACING=true
SWARM_LANGSMITH_API_KEY=${LS_KEY}
SWARM_LANGSMITH_PROJECT=${LS_PROJ}
ENVEOF
        fi

        chmod 600 "$ENV_FILE"
        ok ".env 已生成（权限 600）"
    else
        ok ".env 已存在，跳过生成"
    fi
fi

# ═══════════════════════════════════════════════════════════════
#  Step 8: Qdrant 本地存储 + 启动
# ═══════════════════════════════════════════════════════════════
info "━━━ Step 8: Qdrant 本地存储 ━━━"

QDRANT_DIR="$HOME/.swarm/qdrant"
QDRANT_BIN_DIR="$HOME/.swarm/bin"
mkdir -p "$QDRANT_DIR" "$QDRANT_BIN_DIR"
ok "Qdrant 本地存储: $QDRANT_DIR"

if ! curl -sf "http://localhost:6333/collections" >/dev/null 2>&1; then
    if command -v docker >/dev/null 2>&1; then
        info "Docker 启动 Qdrant..."
        docker rm -f swarm-qdrant 2>/dev/null || true
        docker run -d --name swarm-qdrant -p 6333:6333 -p 6334:6334 \
            -v "$QDRANT_DIR:/qdrant/storage" qdrant/qdrant >/dev/null || warn "Docker Qdrant 启动失败"
    fi
    if ! curl -sf "http://localhost:6333/collections" >/dev/null 2>&1; then
        if [[ ! -x "$QDRANT_BIN_DIR/qdrant" ]]; then
            ARCH="$(uname -m)"
            case "$ARCH" in
                arm64) QD_ARCH="aarch64-apple-darwin" ;;
                x86_64) QD_ARCH="x86_64-apple-darwin" ;;
                *) QD_ARCH="" ;;
            esac
            if [[ -n "$QD_ARCH" ]]; then
                info "下载 Qdrant 二进制 ($QD_ARCH)..."
                curl -fsSL "https://github.com/qdrant/qdrant/releases/download/v1.13.2/qdrant-${QD_ARCH}.tar.gz" \
                    | tar -xz -C /tmp && mv /tmp/qdrant "$QDRANT_BIN_DIR/qdrant" && chmod +x "$QDRANT_BIN_DIR/qdrant"
            fi
        fi
        if [[ -x "$QDRANT_BIN_DIR/qdrant" ]]; then
            info "启动 Qdrant 二进制 (6333)..."
            nohup env QDRANT__STORAGE__STORAGE_PATH="$QDRANT_DIR" "$QDRANT_BIN_DIR/qdrant" \
                >> "$PROJECT_ROOT/qdrant.log" 2>&1 &
            sleep 2
        fi
    fi
    if curl -sf "http://localhost:6333/collections" >/dev/null 2>&1; then
        ok "Qdrant 已启动 (http://localhost:6333)"
    else
        warn "Qdrant 未启动 — 预处理将跳过向量嵌入阶段"
    fi
else
    ok "Qdrant 已在运行"
fi

# ═══════════════════════════════════════════════════════════════
#  Step 9: 启动服务
# ═══════════════════════════════════════════════════════════════
info "━━━ Step 9: 启动 Swarm 服务 ━━━"

# 加载 .env 到当前 shell
set -a; source "$PROJECT_ROOT/.env" 2>/dev/null || true; set +a

# 检查端口
PORT="${SWARM_PORT:-8420}"
if lsof -ti:$PORT &>/dev/null 2>/dev/null; then
    warn "端口 $PORT 已被占用，尝试停止..."
    lsof -ti:$PORT | xargs kill -9 2>/dev/null || true
    sleep 1
fi

info "启动 uvicorn (端口 $PORT)..."
export PATH="$HOME/.local/bin:$PATH"

# 后台启动
nohup "$VENV_DIR/bin/uvicorn" swarm.api.app:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level info \
    > "$PROJECT_ROOT/swarm.log" 2>&1 &

SERVER_PID=$!
sleep 3

# 健康检查
for i in 1 2 3 4 5; do
    if curl -sf "http://localhost:$PORT/api/status" >/dev/null 2>&1; then
        ok "Swarm 服务已启动 (PID=$SERVER_PID, http://localhost:$PORT)"
        echo ""
        echo "╔══════════════════════════════════════════╗"
        echo "║  🐝 Swarm 蜂群 AI 编程智能体系统         ║"
        echo "║  http://localhost:$PORT                   ║"
        echo "║  PID: $SERVER_PID                          ║"
        echo "║  日志: tail -f $PROJECT_ROOT/swarm.log     ║"
        echo "║  重载: bash scripts/restart-api.sh         ║"
        echo "║  停止: bash scripts/stop-api.sh            ║"
        echo "╠══════════════════════════════════════════╣"
        echo "║  1. 添加项目 → 预处理 → 新建 Brain 任务              ║"
        echo "║  2. Worker Tab / swarm worker-run — Phase0 直跑      ║"
        echo "║  3. approve → LEARN + 知识库增量入队                 ║"
        echo "║  CLI: swarm submit -p <id> --watch                   ║"
        echo "║  文档: README.md · docs/Swarm_System.html            ║"
        echo "╚══════════════════════════════════════════╝"
        exit 0
    fi
    sleep 2
done

# 如果健康检查失败，输出日志
warn "服务启动可能有问题，最近日志:"
tail -20 "$PROJECT_ROOT/swarm.log" 2>/dev/null || true
echo ""
echo "手动启动: cd $PROJECT_ROOT && source .env && .venv/bin/uvicorn swarm.api.app:app --port $PORT"
exit 1
