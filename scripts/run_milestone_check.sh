#!/usr/bin/env bash
# Phase 0/1 里程碑验收 — 包装 benchmark_accept_rate.py
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT_ID="${1:-}"
PHASE="${2:-0}"
THRESHOLD="${SWARM_MILESTONE_THRESHOLD:-0.6}"
API_URL="${SWARM_API_URL:-http://127.0.0.1:8420}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "用法: bash scripts/run_milestone_check.sh <project-id> [phase=0|1]"
  echo "环境: SWARM_API_URL, SWARM_MILESTONE_THRESHOLD (默认 0.6)"
  exit 1
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "错误: 未找到 .venv，请先 bash setup.sh"
  exit 1
fi

exec "$ROOT/.venv/bin/python" "$ROOT/scripts/benchmark_accept_rate.py" \
  --api-url "$API_URL" \
  --project-id "$PROJECT_ID" \
  --phase "$PHASE" \
  --threshold "$THRESHOLD"
