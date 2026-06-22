#!/usr/bin/env bash
# 重启 Swarm Web API — 代码 / .env / LangSmith 配置变更后使用
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${SWARM_PORT:-8420}"
PID_DIR="${HOME}/.swarm/pids"
VENV="${PROJECT_ROOT}/.venv"

log() { echo "[swarm] $*"; }

"${PROJECT_ROOT}/scripts/stop-api.sh"

if [[ ! -x "${VENV}/bin/uvicorn" ]]; then
  log "错误: 未找到 ${VENV}/bin/uvicorn，请先运行 bash setup.sh"
  exit 1
fi

mkdir -p "$PID_DIR"

cd "$PROJECT_ROOT"
set -a
# shellcheck disable=SC1091
source "${PROJECT_ROOT}/.env" 2>/dev/null || true
set +a

log "启动 Swarm API (端口 ${PORT})..."
# PYTHONUNBUFFERED=1: stdout 重定向到文件时默认块缓冲，导致 worker 早期日志
# （准备/选沙箱模板等）延迟数十秒甚至任务跑完才落盘，排障时看不到。
# 设为无缓冲让日志实时进 swarm.log。
if [[ "${SWARM_DETACH_SESSION:-0}" == "1" ]]; then
  # 固化原 /tmp/start_swarm.sh 的脱离能力：在独立 session 中启动，脱离父进程组
  # （如 Hermes/编排器）。父进程组收到 SIGTERM 时不会连累 uvicorn 一起被杀。
  # macOS 无 setsid 命令，用 .venv 的 python 调 os.setsid() 自建新 session 后
  # exec uvicorn（exec 不换 PID，$! 仍指向最终的 uvicorn，pidfile 有效）。
  nohup "${VENV}/bin/python" -c '
import os, sys
os.setsid()
os.environ["PYTHONUNBUFFERED"] = "1"
os.execv(sys.argv[1], sys.argv[1:])
' "${VENV}/bin/uvicorn" swarm.api.app:app \
    --host 0.0.0.0 --port "${PORT}" --log-level info \
    >> "${PROJECT_ROOT}/swarm.log" 2>&1 &
else
  nohup env PYTHONUNBUFFERED=1 "${VENV}/bin/uvicorn" swarm.api.app:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --log-level info \
    >> "${PROJECT_ROOT}/swarm.log" 2>&1 &
fi
api_pid=$!
echo "$api_pid" > "${PID_DIR}/swarm.pid"
disown -h "$api_pid" 2>/dev/null || true

for _ in $(seq 1 25); do
  if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    log "Swarm API 已重启 → http://localhost:${PORT} (pid=${api_pid})"
    log "日志: tail -f ${PROJECT_ROOT}/swarm.log"
    # 可选：打印 LangSmith 是否 active
    if command -v python3 >/dev/null 2>&1; then
      ls_status="$("${VENV}/bin/python" -c "
from swarm.config.settings import reload_config
from swarm.tracing import configure_langsmith, langsmith_status
reload_config()
configure_langsmith(reload=True)
import json
print(json.dumps(langsmith_status(), ensure_ascii=False))
" 2>/dev/null || echo '{}')"
      log "LangSmith: ${ls_status}"
    fi
    exit 0
  fi
  sleep 1
done

log "错误: API 启动失败，查看 ${PROJECT_ROOT}/swarm.log"
tail -25 "${PROJECT_ROOT}/swarm.log" 2>/dev/null || true
exit 1
