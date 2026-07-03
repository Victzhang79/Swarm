#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# e2e_three_clean.sh —— E2E「三清」常驻脚本（治本：别每轮手工归档/截断/清沙箱）
#
# 三清 = 每轮开跑前的【日志侧】清理（与项目侧 e2e_reset_baseline.sh 分工，互不重叠）：
#   ① 归档上一轮 swarm.log + 所有沙箱 jsonl → logs_archive/<tag>_<ts>/（先备份，可回溯）
#   ② 轮转（清空）swarm.log —— 让本轮主日志从零开始，三盯不被上轮污染
#   ③ 清空 ~/.swarm/sandbox_logs/*.jsonl —— 本轮沙箱日志干净
#
# fail-closed 守则：
#   - 先归档后截断（永不无备份删日志）；归档失败则中止，不动原文件。
#   - 只在 ~/.swarm/sandbox_logs 目录内删 *.jsonl，路径写死，拒绝任何目录穿越。
#   - 不碰项目工作树（那是 e2e_reset_baseline.sh 的职责，由 e2e_run.sh 调）。
#
# 用法:  scripts/e2e_three_clean.sh <tag>        例: scripts/e2e_three_clean.sh round17
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
TAG="${1:?用法: e2e_three_clean.sh <tag>  (例: round17)}"
PKG_DIR="/Users/zhangyanrui/LLM/swarm/swarm"
SBX_DIR="$HOME/.swarm/sandbox_logs"
cd "$PKG_DIR" || { echo "[three-clean] ❌ 找不到包目录 $PKG_DIR"; exit 1; }

TS="$(date '+%Y%m%d_%H%M%S')"
ARCH="logs_archive/${TAG}_${TS}"
mkdir -p "$ARCH"

# ── ① 归档（先备份，任何一步失败即中止，绝不在无备份下截断）──
archived=0
if [ -f swarm.log ]; then
  cp -p swarm.log "$ARCH/swarm.log" || { echo "[three-clean] ❌ 归档 swarm.log 失败，中止（不截断）"; exit 1; }
  archived=$((archived+1))
fi
if [ -d "$SBX_DIR" ]; then
  # find 无匹配也返回 0（不像 ls *.jsonl 无匹配返回非零）—— 避免空沙箱目录下 pipefail+set-e 误退
  n=$(find "$SBX_DIR" -maxdepth 1 -name '*.jsonl' -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "${n:-0}" -gt 0 ]; then
    mkdir -p "$ARCH/sandbox_logs"
    cp -p "$SBX_DIR"/*.jsonl "$ARCH/sandbox_logs/" || { echo "[three-clean] ❌ 归档沙箱 jsonl 失败，中止（不清理）"; exit 1; }
    archived=$((archived+n))
  fi
fi
echo "[three-clean] ✓ ① 已归档 $archived 个日志 → $ARCH"

# ── ② 轮转 swarm.log ──
if [ -f swarm.log ]; then : > swarm.log; echo "[three-clean] ✓ ② swarm.log 已清空（备份在 $ARCH/swarm.log）"; fi

# ── ③ 清沙箱 jsonl（路径写死，只删本目录 *.jsonl）──
if [ -d "$SBX_DIR" ]; then
  find "$SBX_DIR" -maxdepth 1 -name '*.jsonl' -type f -delete 2>/dev/null || true
  left=$(find "$SBX_DIR" -maxdepth 1 -name '*.jsonl' -type f 2>/dev/null | wc -l | tr -d ' ')
  echo "[three-clean] ✓ ③ 沙箱 jsonl 已清（剩 ${left:-0}，备份在 $ARCH/sandbox_logs/）"
fi

echo "[three-clean] ✅ 三清完成 tag=$TAG。下一步: soak 探活 → restart-api → e2e_run.sh"
