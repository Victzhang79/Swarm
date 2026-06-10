#!/usr/bin/env bash
# 运行全部单元测试（需在项目根目录已 pip install -e .）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="${VENV:-$ROOT/.venv}"
PY="${VENV}/bin/python"
if [[ ! -x "$PY" && -x "$ROOT/../.venv/bin/python" ]]; then
  PY="$ROOT/../.venv/bin/python"
fi
if [[ $# -gt 0 ]]; then
  exec "$PY" -m pytest "$@"
else
  exec "$PY" -m pytest test/ "$@"
fi
