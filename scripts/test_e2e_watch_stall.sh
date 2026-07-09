#!/usr/bin/env bash
# 单测：e2e_watch.sh 卡死判据(stall_step) —— round37 误杀根因回归。
# 旧逻辑用 status|completed 判活性 → 基础子任务 900s 超时期间 completed 不变即误判卡死取消。
# 新逻辑：活性 = 日志行增长 或 status/completed 变化；真冻结(零新日志行)才累计。
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1090
source "$HERE/e2e_watch.sh"   # BASH_SOURCE 守卫 → 只加载函数，不跑 main

pass=0; fail=0
check(){ # desc expected actual
  if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "  ✓ $1"
  else fail=$((fail+1)); echo "  ✗ $1: 期望[$2] 实得[$3]"; fi
}

# ① round37 现场：日志在增长(Worker 心跳)、completed 不变、DISPATCHING → 必须清零(旧逻辑会累计→误杀)
check "log_grew=1 completed不变 DISPATCHING → 清零" 0 "$(stall_step 1 0 DISPATCHING 39)"
# ② 真冻结：零日志增长 + 无状态变化 + 执行态 → 累加(仍能抓到真卡死)
check "log_grew=0 DISPATCHING → 累加"               40 "$(stall_step 0 0 DISPATCHING 39)"
# ③ status/completed 变化 → 清零
check "changed=1 → 清零"                            0 "$(stall_step 0 1 DISPATCHING 39)"
# ④ 非执行态(PLANNING 等长安静期)零活性 → 不累计(避免误杀 ultra 长规划)
check "非执行态零活性 → 清零"                        0 "$(stall_step 0 0 PLANNING 39)"
# ⑤ VERIFYING 零活性 → 累加(执行态)
check "VERIFYING 零活性 → 累加"                      5 "$(stall_step 0 0 VERIFYING 4)"
# ⑥ 连续冻结单调 +1
check "MERGING 连续冻结单调+1"                       2 "$(stall_step 0 0 MERGING 1)"

echo "── 结果: pass=$pass fail=$fail ──"
[ "$fail" -eq 0 ]
