#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# e2e_mirrors.sh —— E2E「三面镜像 + 随跑日志」常驻记录器（治本：别每轮现造监视脚本）
#
# 为什么在 triple_watch 之外还要它：
#   triple_watch 是「每 10min 一段合并快照」，适合终态复盘；但 live 陪跑需要「按来源分面、
#   连续追加」的细粒度镜像，配合哨兵事件唤醒做「有情况即核对上报」。三面各自独立文件、
#   持续 append（不是每轮现造 shell 后台单点轮询）。对齐记忆 read-logs-holistically：
#   分面镜像后仍必须「模板去重式连续通读」，别 grep 戳（grep 会漏 pom owner 预警/BLOCKED 首伤）。
#
# 三面镜像（各自常驻 daemon，持续 append 到独立文件）:
#   ①swarm_mirror.log    —— swarm.log 的相位/路由/异常/高信号关键词行镜像（tail -F 过滤）
#   ②sandbox_mirror.log  —— 每子任务沙箱执行相位/L1/BLOCKED/错误明细（镜像 swarm.log 的
#                            Worker(st-xxx) 行）。★round36 教训：远程热池沙箱(192.168.60.106:3000)
#                            执行明细写 swarm.log 不落本地 jsonl；旧版只轮询本地 jsonl→全程盲(显 0)★
#   ③artifact_mirror.log —— RuoYi 工作树变更文件快照（45s，只记变化：产物落盘真相非"跑没跑"）
# 外加 journal.md —— 随跑日志（人手写：每次哨兵唤醒→三面交叉核对→追加一段）
#
# 用法:  scripts/e2e_mirrors.sh <task_id> [rounds_tag] [ruoyi_path]
#   例:  scripts/e2e_mirrors.sh 534c8f30-... round35
#   （脚本自身即刻返回；三面 daemon 已 nohup+disown 后台常驻，打印各 pid）
#
# 产出目录: logs_archive/process/<tag>_mirrors/
#   swarm_mirror.log · sandbox_mirror.log · artifact_mirror.log · journal.md · mirror_*.sh(生成的daemon脚本)
# 停止: 每轮收尾时 `pkill -f <tag>_mirrors` 或 three_clean 下一轮归档前清理。
#
# 配套哨兵（唤醒模型来核对，非本脚本职责，起跑时另建 run_in_background）：轮询 task 状态 +
#   扫 swarm_mirror.log 增量字节命中高信号词(DISPATCH/VERIFY_RUNTIME/VERIFY_L2/MERGE/escalate/
#   BLOCKED/command not found/__RC__/reactor error/DELIVER 拒绝/ACCEPT)即退出唤醒；否则 ~30min tick。
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
TID="${1:?用法: e2e_mirrors.sh <task_id> [tag] [ruoyi_path]}"
TAG="${2:-run}"
RUOYI="${3:-/Users/zhangyanrui/LLM/swarm/e2e-projects/RuoYi}"
PKG_DIR="/Users/zhangyanrui/LLM/swarm/swarm"
SWARMLOG="$PKG_DIR/swarm.log"
SBDIR="$HOME/.swarm/sandbox_logs"
MDIR="$PKG_DIR/logs_archive/process/${TAG}_mirrors"
mkdir -p "$MDIR"

# 高信号相位/异常关键词（通用多栈，勿写死语言）——覆盖相位机、恢复阶梯、编译/脚手架/覆盖/申报面
# G2-3（round38c 主题G P0）：补 skills-telemetry/模型调用/breaker/ledger——round38
# 判读面明说 grep skills-telemetry，旧 KW 却滤掉该行=镜像失明误判"0 调用"
KW='阶段|PLAN|DISPATCH|CONFIRM|VERIFY|MERGE|DELIVER|ACCEPT|LEARN|escalate|BLOCKED|give_up|covers|申报|baseline|超时|timeout|bisect|哨兵|normalize|pom owner|__RC__|command not found|mvn |migration|skipped|unverified|recursion|replan|apply_ok|stall|reactor|scaffold|脚手架|scope|writable|skills-telemetry|模型调用|breaker|ledger|MANIFEST-SYNTH|wallclock'

# ---- 随跑日志（若不存在才建，避免重跑覆盖）----
JOURNAL="$MDIR/journal.md"
if [ ! -f "$JOURNAL" ]; then
  {
    echo "# ${TAG} 随跑日志 (task=${TID:0:8})"
    echo
    echo "> 三面 mirror + 哨兵唤醒 + 本随跑日志。哨兵有情况→读三面交叉核对(swarm相位×沙箱事件×产物落盘)→在此追加一段。"
    echo "> 判读按机制不按子任务ID。终态后必须模板去重式连续通读，别 grep 戳。"
    echo
    echo "---"
  } > "$JOURNAL"
fi

# ---- Mirror ①: swarm.log 相位/路由/异常 ----
cat > "$MDIR/mirror_swarm.sh" <<EOF
#!/bin/bash
tail -n0 -F "$SWARMLOG" 2>/dev/null | grep --line-buffered -E '$KW' >> "$MDIR/swarm_mirror.log"
EOF

# ---- Mirror ②: 沙箱执行明细（镜像 swarm.log 的 Worker(...) 行）----
# 远程热池沙箱不落本地 jsonl，执行真相在 swarm.log 的 Worker(st-xxx): [Ns][PHASE] 行（相位机/
# L1 结果/BLOCKED/沙箱错误/pull-back）。与 Mirror ① 同 tail -F|grep 结构（pkill 一致可停）。
# 本地沙箱轮的 jsonl(若有)其事件也经 executor 打进 swarm.log，故单源即可，不再脆弱轮询本地 jsonl。
cat > "$MDIR/mirror_sandbox.sh" <<EOF
#!/bin/bash
tail -n0 -F "$SWARMLOG" 2>/dev/null | grep --line-buffered -E \
  'Worker\(|远程沙箱|镜像 worker|pull-back|internal_pkg_not_built|blocked_on|L1 验证结果|沙箱集成编译|沙箱镜像选择' \
  >> "$MDIR/sandbox_mirror.log"
EOF

# ---- Mirror ③: 产物快照（只记变化）----
cat > "$MDIR/mirror_artifact.sh" <<EOF
#!/bin/bash
prev=""
while true; do
  ts=\$(date '+%H:%M:%S')
  cnt=\$(git -C "$RUOYI" status --short 2>/dev/null | wc -l | tr -d ' ')
  stat=\$(git -C "$RUOYI" diff --stat 2>/dev/null | tail -n1)
  sig="\$cnt|\$stat"
  if [ "\$sig" != "\$prev" ]; then
    echo "[\$ts] 变更文件=\$cnt | \$stat" >> "$MDIR/artifact_mirror.log"
    git -C "$RUOYI" status --short 2>/dev/null | head -40 | sed "s/^/[\$ts]   /" >> "$MDIR/artifact_mirror.log"
    prev="\$sig"
  fi
  sleep 45
done
EOF

chmod +x "$MDIR"/mirror_*.sh
nohup bash "$MDIR/mirror_swarm.sh"    </dev/null >/dev/null 2>&1 & disown; P1=$!
nohup bash "$MDIR/mirror_sandbox.sh"  </dev/null >/dev/null 2>&1 & disown; P2=$!
nohup bash "$MDIR/mirror_artifact.sh" </dev/null >/dev/null 2>&1 & disown; P3=$!

echo "[e2e-mirrors] ✓ 三面镜像已常驻 tag=$TAG task=${TID:0:8}"
echo "  ①swarm    pid=$P1 → $MDIR/swarm_mirror.log"
echo "  ②sandbox  pid=$P2 → $MDIR/sandbox_mirror.log"
echo "  ③artifact pid=$P3 → $MDIR/artifact_mirror.log"
echo "  随跑日志: $JOURNAL"
echo "[e2e-mirrors] 下一步: 另起哨兵(run_in_background)按状态跃迁/高信号词唤醒；停止用 pkill -f ${TAG}_mirrors"
