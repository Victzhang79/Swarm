#!/usr/bin/env bash
# 停止 Swarm Web API（uvicorn，默认 8420）
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${SWARM_PORT:-8420}"
PID_DIR="${HOME}/.swarm/pids"
PID_FILE="${PID_DIR}/swarm.pid"

log() { echo "[swarm] $*"; }

stopped=0

if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    log "停止 API 进程 (pid=${pid})..."
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.3
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

PORT_PIDS=$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "${PORT_PIDS}" ]]; then
  log "停止占用端口 ${PORT} 的进程: ${PORT_PIDS}"
  kill ${PORT_PIDS} 2>/dev/null || true
  sleep 1
  for pid in ${PORT_PIDS}; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  stopped=1
fi

if [[ "$stopped" -eq 1 ]]; then
  log "Swarm API 已停止 (端口 ${PORT})"
else
  log "Swarm API 未在运行 (端口 ${PORT})"
fi
