#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# e2e_triple_watch.sh —— E2E「三盯」常驻记录器（治本：别每轮现造监视脚本）
#
# 三盯（对齐记忆 read-logs-holistically）：
#   ①任务主日志 swarm.log 的阶段/路由/异常   ②每沙箱 ~/.swarm/sandbox_logs/<sid>.jsonl 尾
#   ③子任务态分布(completed/total) + 产物落盘计数
# 外加【异常信号】自动捞：stall / replan / VERIFY_L2 fail / 模型退化(!!!!/OkHttp 循环) / 900s 超时 / recursion_limit
#
# 用法:  scripts/e2e_triple_watch.sh <task_id> [interval_sec=600] [rounds_tag]
#   例:  nohup scripts/e2e_triple_watch.sh 996db614-... 600 round17 >/dev/null 2>&1 &
#
# 产出（持久，可回溯）: logs_archive/process/<tag>_<task8>.log  —— 每周期一段三盯快照
# 退出: 命中终态(DONE/FAILED/PARTIAL/CANCELLED) 即写终态段并退出 20；否则一直记到进程被杀。
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
TID="${1:?用法: e2e_triple_watch.sh <task_id> [interval_sec] [tag]}"
INTERVAL="${2:-600}"
TAG="${3:-run}"
PKG_DIR="/Users/zhangyanrui/LLM/swarm/swarm"
TOK="$(cat ~/.swarm/cli_token 2>/dev/null)"
SBX_DIR="$HOME/.swarm/sandbox_logs"
OUT_DIR="$PKG_DIR/logs_archive/process"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/${TAG}_${TID:0:8}.log"
cd "$PKG_DIR" || exit 1

py=".venv/bin/python"; [ -x "$py" ] || py="python3"

snap() {
  {
    echo "════════════════════════════════════════════════════════════════"
    echo "[$(date '+%F %H:%M:%S')] 三盯快照 task=${TID:0:8} tag=$TAG"
    # ── ③ 任务态 ──
    raw=$(curl -s -m 8 -H "Authorization: Bearer $TOK" "http://localhost:8420/api/tasks/$TID" | "$py" -c "import sys,json
try:
 d=json.load(sys.stdin)['task']; print(f\"status={d.get('status')} completed={d.get('completed_subtasks')}/{d.get('subtask_count')} phase={d.get('current_phase') or d.get('phase') or '-'}\")
except Exception as e: print('task-api-ERR',e)" 2>/dev/null)
    echo "③ 任务态: $raw"
    # ── ① 主日志：最近阶段/路由 + 异常信号 ──
    echo "① swarm.log 最近路由/阶段:"
    grep -aE "\[ROUTE\]|→ *(PLAN|DISPATCH|MONITOR|MERGE|VERIFY_L2|HANDLE_FAILURE|REPLAN)|\[MERGE\]|\[VERIFY_L2\]" swarm.log 2>/dev/null | tail -6 | sed 's/^/    /'
    echo "① 异常信号(本周期新增计数):"
    # 注意: "未 stall" 是健康流式日志, 排除掉避免误报
    for sig in "检测到.*stall\|已 stall\|stalled" "触发.*replan\|重新规划\|全量重拆" "补丁.*损坏\|git apply --check failed\|apply_ok=False" "!!!!\|OkHttp.*OkHttp" "超预算 1500s\|超时 900s\|900s 超时" "recursion_limit\|递归上限\|迭代上限"; do
      n=$(grep -aE "$sig" swarm.log 2>/dev/null | grep -avc "未 stall")
      [ "${n:-0}" -gt 0 ] && echo "    ⚠ /$sig/ 累计=$n"
    done
    # ── ② 沙箱 jsonl：数量 + 每个尾行 event ──
    if [ -d "$SBX_DIR" ]; then
      cnt=$(ls "$SBX_DIR"/*.jsonl 2>/dev/null | wc -l | tr -d ' ')
      echo "② 沙箱日志数: $cnt"
      for f in $(ls -t "$SBX_DIR"/*.jsonl 2>/dev/null | head -8); do
        sid=$(basename "$f" .jsonl)
        last=$("$py" -c "import sys,json
try:
 ls=[l for l in open('$f') if l.strip()]; d=json.loads(ls[-1]); print(d.get('event') or d.get('type') or d.get('phase') or list(d.keys())[:3])
except Exception: print('(空/坏)')" 2>/dev/null)
        echo "    ${sid:0:12} → $last"
      done
    else
      echo "② 沙箱日志目录暂无"
    fi
    # ── 产物落盘（RuoYi 工作树 git 变更文件数，粗看是否真落盘）──
    proj="/Users/zhangyanrui/LLM/swarm/e2e-projects/RuoYi"
    if [ -d "$proj/.git" ]; then
      chg=$(git -C "$proj" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
      echo "④ RuoYi 工作树变更文件数: $chg"
    fi
    echo ""
  } >> "$OUT"
}

snap
elapsed=0
while true; do
  sleep 30; elapsed=$((elapsed+30))
  st=$(curl -s -m 8 -H "Authorization: Bearer $TOK" "http://localhost:8420/api/tasks/$TID" | "$py" -c "import sys,json
try: print(json.load(sys.stdin)['task'].get('status'))
except Exception: print('ERR')" 2>/dev/null)
  case "$st" in
    DONE*|FAILED*|PARTIAL*|CANCELLED*)
      snap
      echo "[$(date '+%F %H:%M:%S')] === 终态 $st，记录器退出 ===" >> "$OUT"
      exit 20 ;;
  esac
  if [ "$elapsed" -ge "$INTERVAL" ]; then snap; elapsed=0; fi
done
