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

WORKDIR /app

# 运行时系统依赖：curl 用于 healthcheck；git 供 worker 在沙箱外的本地操作（difflib 兜底，但保留）
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git \
    && rm -rf /var/lib/apt/lists/*

# 拷贝已安装的依赖 + swarm 包
COPY --from=builder /install /usr/local
# 拷贝源码（静态资源 WebUI、scripts 等运行时需要的非包文件）
COPY . /app

# 非 root 运行
RUN useradd -m -u 10001 swarm && chown -R swarm:swarm /app
USER swarm

EXPOSE 8420

# 健康检查：/api/health（公开端点，不需鉴权）
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://localhost:8420/api/health || exit 1

# 启动：uvicorn 起 API；app.py on_startup 钩子幂等建全部表（与 init_db 一致），
# 故无需单独 init_db step。沙箱池开关由 env 控制。
CMD ["python", "-m", "uvicorn", "swarm.api.app:app", "--host", "0.0.0.0", "--port", "8420"]
