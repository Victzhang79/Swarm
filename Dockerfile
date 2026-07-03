# Swarm 自身镜像（API + Brain 编排 + Worker 子进程 + 知识库 + 记忆）。
# 注意：本镜像是 Swarm 服务栈，与 CubeSandbox（独立远程沙箱执行服务器）无关——
# Worker 通过 e2b SDK 经 dev_sidecar 代理连远程 CubeSandbox，连接参数走 env 注入。
#
# 基础镜像对齐 CI（python 3.12）。多阶段构建：builder 装依赖，runtime 瘦身。

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 系统构建依赖（psycopg[binary] 自带 libpq，无需 libpq-dev；保留 build-essential
# 以防个别 wheel 需本地编译，runtime 阶段不带）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# 先只 copy 依赖声明，利用层缓存（依赖不变则不重装）
COPY pyproject.toml README.md ./
# 包代码（package-dir "swarm"="."，故源码在构建上下文根目录）
COPY . .

# 装到独立 prefix，便于 runtime 阶段整体拷贝
RUN pip install --prefix=/install .

# ─── runtime ───────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8420

# ⚠️ 关键：工作目录绝不能是含 swarm 源码（尤其 types.py）的目录。
# 否则 `python -m uvicorn` 会把 cwd 放进 sys.path[0]，本仓库的 swarm/types.py
# 会遮蔽 Python 标准库 types 模块 → stdlib enum.py 的 `from types import MappingProxyType`
# 触发循环导入 → 容器无限重启（本机 editable+包内导入不暴露，容器才炸）。
# swarm 已 pip install 进 site-packages，运行时无需源码根在 path 里；静态资源(WebUI)
# 已随包 package-data 打包。故用独立空工作目录 /srv。
WORKDIR /srv

# 运行时系统依赖：curl 用于 healthcheck；git 供 worker 本地操作
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git \
    && rm -rf /var/lib/apt/lists/*

# 拷贝已安装的依赖 + swarm 包（含打包进去的 WebUI 静态资源）
COPY --from=builder /install /usr/local

# 运行时辅助脚本（init_db 等）放到不在 sys.path 顶层的位置，按需调用；
# 不放进 cwd，避免任何源码文件（types.py 等）遮蔽标准库。
COPY scripts /opt/swarm/scripts

# 非 root 运行
RUN useradd -m -u 10001 swarm && chown -R swarm:swarm /srv /opt/swarm
USER swarm

EXPOSE 8420

# 健康检查：/api/health/ready（就绪探针，公开可达、真实探测 PG/Redis[启用时]/Qdrant）。
# 不用 /api/health（纯存活，不探依赖→依赖宕机仍假绿）；不用 /api/status（需鉴权，#21）。
# start-period 给足首启跑迁移+建表+连依赖的时间；依赖不可达时返 503→容器判 unhealthy。
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=6 \
    CMD curl -fsS http://localhost:8420/api/health/ready || exit 1

# 启动：uvicorn 起 API（从 site-packages 加载 swarm 包，cwd=/srv 无源码遮蔽）；
# app.py on_startup 钩子先跑 run_migrations（幂等：stamp schema_version）再 ensure_tables
# 建全部表（与 init_db 同一迁移+建表路径），无需单独 init_db step。
CMD ["python", "-m", "uvicorn", "swarm.api.app:app", "--host", "0.0.0.0", "--port", "8420"]
