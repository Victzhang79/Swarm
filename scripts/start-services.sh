#!/usr/bin/env bash
# 启动 Swarm 依赖服务：Qdrant + Web API
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${SWARM_PORT:-8420}"
QDRANT_DIR="${HOME}/.swarm/qdrant"
QDRANT_BIN="${HOME}/.swarm/bin/qdrant"
PID_DIR="${HOME}/.swarm/pids"
VENV="${PROJECT_ROOT}/.venv"

log() { echo "[swarm] $*"; }

mkdir -p "$PID_DIR"

daemonize() {
  # 脱离 Cursor/终端会话，避免父 shell 退出时带走子进程
  local name="$1"
  shift
  nohup "$@" >> "${PROJECT_ROOT}/${name}.log" 2>&1 &
  local pid=$!
  echo "$pid" > "${PID_DIR}/${name}.pid"
  disown -h "$pid" 2>/dev/null || true
  echo "$pid"
}

# ── Qdrant ──
if curl -sf "http://127.0.0.1:6333/collections" >/dev/null 2>&1; then
  log "Qdrant 已在运行 (6333)"
else
  # 优先 Docker（跨平台最省事），无 Docker 再下原生二进制
  if command -v docker >/dev/null 2>&1; then
    log "Docker 启动 Qdrant..."
    docker rm -f swarm-qdrant >/dev/null 2>&1 || true
    docker run -d --name swarm-qdrant -p 6333:6333 -p 6334:6334 \
      -v "${QDRANT_DIR}:/qdrant/storage" qdrant/qdrant >/dev/null 2>&1 || log "Docker Qdrant 启动失败，回退二进制"
    sleep 2
  fi
  if ! curl -sf "http://127.0.0.1:6333/collections" >/dev/null 2>&1; then
    mkdir -p "$QDRANT_DIR" "$(dirname "$QDRANT_BIN")"
    if [[ ! -x "$QDRANT_BIN" ]]; then
      OS="$(uname -s)"; ARCH="$(uname -m)"
      QD_ARCH=""
      case "$OS" in
        Darwin)
          case "$ARCH" in
            arm64) QD_ARCH="aarch64-apple-darwin" ;;
            x86_64) QD_ARCH="x86_64-apple-darwin" ;;
          esac ;;
        Linux)
          case "$ARCH" in
            aarch64|arm64) QD_ARCH="aarch64-unknown-linux-gnu" ;;
            x86_64) QD_ARCH="x86_64-unknown-linux-gnu" ;;
          esac ;;
      esac
      if [[ -n "${QD_ARCH}" ]]; then
        log "下载 Qdrant ${QD_ARCH}..."
        curl -fsSL "https://github.com/qdrant/qdrant/releases/download/v1.13.2/qdrant-${QD_ARCH}.tar.gz" \
          | tar -xz -C /tmp
        mv /tmp/qdrant "$QDRANT_BIN"
        chmod +x "$QDRANT_BIN"
      else
        log "无法自动下载 Qdrant (OS=$OS arch=$ARCH)，请手动安装或用 Docker"
      fi
    fi
    if [[ -x "$QDRANT_BIN" ]]; then
      log "启动 Qdrant..."
      daemonize qdrant env QDRANT__STORAGE__STORAGE_PATH="$QDRANT_DIR" "$QDRANT_BIN" >/dev/null
      sleep 2
    fi
  fi
  if curl -sf "http://127.0.0.1:6333/collections" >/dev/null 2>&1; then
    log "Qdrant 已启动"
  else
    log "警告: Qdrant 未启动，预处理将跳过向量嵌入"
  fi
fi

# ── Swarm API ──
if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
  log "Swarm API 已在运行 (http://localhost:${PORT})"
  log "如需重载代码或 .env，请运行: bash scripts/restart-api.sh"
  exit 0
fi

if [[ ! -x "${VENV}/bin/uvicorn" ]]; then
  log "错误: 未找到 ${VENV}/bin/uvicorn，请先运行 setup.sh"
  exit 1
fi

# 仅停止占用端口的 LISTEN 进程
OLD_PIDS=$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "${OLD_PIDS}" ]]; then
  log "停止旧 API 进程: ${OLD_PIDS}"
  kill -9 ${OLD_PIDS} 2>/dev/null || true
  sleep 1
fi

cd "$PROJECT_ROOT"
set -a
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/.env" 2>/dev/null || true
set +a

log "启动 Swarm API (端口 ${PORT})..."
# PYTHONUNBUFFERED=1: 避免 stdout 重定向到文件时块缓冲导致 worker 早期日志延迟落盘
daemonize swarm env PYTHONUNBUFFERED=1 "${VENV}/bin/uvicorn" swarm.api.app:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --log-level info >/dev/null

for _ in $(seq 1 20); do
  if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    log "Swarm API 已启动 → http://localhost:${PORT}"
    log "日志: tail -f ${PROJECT_ROOT}/swarm.log"
    exit 0
  fi
  sleep 1
done

log "错误: API 启动失败，查看 ${PROJECT_ROOT}/swarm.log"
tail -20 "${PROJECT_ROOT}/swarm.log" || true
exit 1
