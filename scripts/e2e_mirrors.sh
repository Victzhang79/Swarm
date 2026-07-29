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
# ★取证污染治本（26 号文 P0-2 / 路 D #5）★
# 病灶：镜像 tail 的是【全局 swarm.log】→ 多轮镜像互相灌串。实测 round67m 的"本轮镜像"里
# round67m2 的行数(844)比本轮自己(685)还多；三轮的 swarm_mirror.log 在同一秒收到同一条
# [RECONCILE] 行。于是 23/24 号文引用的行号指向一个【仍在被写入】的文件，复核者回查必对不上。
#
# ★换源的真实理由（复核 L1 纠正了初版注释里写错的因果）★
# 初版注释称"高信号行不带 task= 标记，按内容过滤做不到"——**这是错的**：文本 formatter
# 是 `%(name)s%(task_suffix)s`（logging_config.py），task 上下文内的行恒带 `[task=xxxxxxxx]`。
# 真实理由是 **全局 swarm.log 会按 10MB 轮转**：复核实测 7/28 的 4396d8ff 在全局日志里
# `[task=4396d8ff]` 已 0 行（轮转掉了），而 per-task 文件仍有 4852 行。
# 本仓注释是可 grep 的因果索引，依据写错会误导后人，故在此更正。
TASKLOG="$PKG_DIR/logs/${TID}.log"
# ★无条件用 per-task 日志（复核 H-1/M2）★
# 初版有个 `[ -f "$TASKLOG" ]` 守卫，不存在就退回全局日志。而 E2E 的动作序是"提交任务 →
# 立刻起镜像"，per-task 文件由 _PerTaskFileHandler 惰性创建（要等第一条带 task ctx 的日志），
# 任务排队时几分钟都不会出现 → **守卫在起跑时刻常态命中 → 整轮锁死在全局日志上**，
# 治本对当轮等于没做，唯一提示还只是一行没人回看的 stdout。
# 而这个守卫是纯负资产：`tail -n0 -F` 本就为"文件尚不存在"设计（复核实测：文件后建也能接上）。
SWARMLOG="$TASKLOG"
MIRROR_SRC="per-task"
if [ ! -f "$TASKLOG" ]; then
  echo "[e2e-mirrors] per-task 日志尚未落地（任务可能在排队）——tail -F 会在它出现时自动接上：$TASKLOG"
fi
SBDIR="$HOME/.swarm/sandbox_logs"
MDIR="$PKG_DIR/logs_archive/process/${TAG}_mirrors"
mkdir -p "$MDIR"

[ -n "$TAG" ] || TAG=run       # 复核 L-4：空 TAG 会让匹配模式退化成 `_mirrors` 横扫所有轮次

# ★启动前清残留★：本脚本尾部一直写着"停止用 pkill"，但收尾没人执行——实测 6 个 tail
# 从 7/28 10:17 起跨三轮一直在跑，把每一轮的日志同时灌进所有轮的镜像。清理必须在启动时
# 做（收尾时做会因为忘记而失效）。
#
# ★但"pkill -f <tag>_mirrors" 杀不掉真正的写入者（对抗双复核独立实证，CRITICAL）★
# daemon 进程树是：bash <tag>_mirrors/mirror_swarm.sh → tail -n0 -F <日志> | grep -E <KW>
# **只有 bash 的 argv 含 <tag>_mirrors**；tail 的 argv 是日志路径、grep 的是 KW 串，
# 重定向不进 argv。复核实证：pkill 之后 tail/grep 存活，且新写入的行照样落进旧 mirror 文件。
# 26 号文实测的"9 个 tail 跨三轮一直在跑"正是这类【孤儿】——旧版 pgrep 根本探测不到它们，
# 脚本却会打印"已清理"给出虚假背书。
# 治法：**每个 daemon wrapper 自带 `trap 'pkill -P $$' TERM INT EXIT`**——wrapper 一死就
# 带走自己的 tail/grep（它俩是该 wrapper 的直接子进程）。wrapper pid 落盘台账供清理时精确
# 杀。清理后 **复查孤儿并如实报告**——绝不静默宣告已清。
# （不用 setsid：`setsid nohup cmd &` 的 `$!` 是 setsid 自身的 pid，setsid fork 后立刻退出，
#  记下的 pid 随即失效、`kill -- -pid` 必然打空——自检实跑已证。trap 方案无此坑且更可移植。）
_PID_FILE="$MDIR/daemons.pid"
_kill_stale_daemons() {
  local killed=0
  if [ -f "$_PID_FILE" ]; then
    while read -r _pid; do
      [ -n "$_pid" ] || continue
      if kill -0 "$_pid" 2>/dev/null; then
        echo "[e2e-mirrors] 清理同 tag 残留 daemon pid=${_pid}（trap 会带走其 tail/grep 子进程）"
        kill -TERM "$_pid" 2>/dev/null || true
        killed=1
      fi
    done < "$_PID_FILE"
    : > "$_PID_FILE"
  fi
  # 兜底：老版本（无 pgid 台账）留下的 wrapper——只杀本脚本生成的 daemon，
  # 匹配收窄到 "<tag>_mirrors/mirror_"（复核 H-2：宽匹配会连【本轮哨兵】一起杀，
  # 哨兵的命令行里必然含 <tag>_mirrors/swarm_mirror.log；人工 tail/less 同理被误杀）
  local _legacy
  _legacy=$(pgrep -f "${TAG}_mirrors/mirror_" 2>/dev/null | tr '\n' ' ')
  if [ -n "$_legacy" ]; then
    echo "[e2e-mirrors] 清理旧版残留 wrapper: $_legacy"
    pkill -f "${TAG}_mirrors/mirror_" 2>/dev/null || true
    killed=1
  fi
  [ "$killed" = 1 ] && sleep 1
  return 0
}
_kill_stale_daemons

# ★清理后复查：孤儿 tail/grep 探测不到 tag，只能按"在 tail 我们的日志源"来找★
_orphans=$(pgrep -fl "tail -n0 -F" 2>/dev/null | grep -F -e "$TASKLOG" -e "$PKG_DIR/swarm.log" || true)
if [ -n "$_orphans" ]; then
  echo "[e2e-mirrors] ⚠️ 仍有孤儿写入进程在 tail 同一日志源（它们会继续往【某个】mirror 文件灌）："
  echo "$_orphans" | sed 's/^/    /'
  echo "    → 本轮取证可能被污染。确认无人陪跑后手工清理: pkill -f 'tail -n0 -F'"
fi
# 跨轮残留（别的 tag 的 mirror 还在跑）只告警不擅杀——可能是别人正在陪跑
_others=$(pgrep -fl "_mirrors/mirror_" 2>/dev/null | grep -v "${TAG}_mirrors" || true)
if [ -n "$_others" ]; then
  _oc=$(echo "$_others" | grep -c .)
  echo "[e2e-mirrors] ⚠️ 检测到【其它轮次】的 mirror daemon 仍在运行（共 $_oc 个，会继续写它们自己的镜像）："
  echo "$_others" | head -5 | sed 's/^/    /'
  [ "$_oc" -gt 5 ] && echo "    …（另有 $((_oc - 5)) 个未列出）"
  echo "    如已收尾请清理: pkill -f '_mirrors/mirror_'"
fi

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
# 被杀时带走 tail/grep 子进程——它们的 argv 里没有 tag，pkill -f 永远够不着（复核 CRITICAL）
trap 'pkill -P \$\$ 2>/dev/null; exit 143' TERM INT
trap 'pkill -P \$\$ 2>/dev/null' EXIT
# ★管道必须放后台再 wait★：bash 在【前台】等待管道时不执行 trap，TERM 要等管道结束才处理，
# 而 tail -F 永不结束 → trap 形同虚设（自检实跑证过：kill wrapper 后 tail 仍在写）。
tail -n0 -F "$SWARMLOG" 2>/dev/null | grep --line-buffered -E '$KW' >> "$MDIR/swarm_mirror.log" &
wait \$!
EOF

# ---- Mirror ②: 沙箱执行明细（镜像 swarm.log 的 Worker(...) 行）----
# 远程热池沙箱不落本地 jsonl，执行真相在 swarm.log 的 Worker(st-xxx): [Ns][PHASE] 行（相位机/
# L1 结果/BLOCKED/沙箱错误/pull-back）。与 Mirror ① 同 tail -F|grep 结构（pkill 一致可停）。
# 本地沙箱轮的 jsonl(若有)其事件也经 executor 打进 swarm.log，故单源即可，不再脆弱轮询本地 jsonl。
cat > "$MDIR/mirror_sandbox.sh" <<EOF
#!/bin/bash
trap 'pkill -P \$\$ 2>/dev/null; exit 143' TERM INT
trap 'pkill -P \$\$ 2>/dev/null' EXIT
tail -n0 -F "$SWARMLOG" 2>/dev/null | grep --line-buffered -E \
  'Worker\(|远程沙箱|镜像 worker|pull-back|internal_pkg_not_built|blocked_on|L1 验证结果|沙箱集成编译|沙箱镜像选择' \
  >> "$MDIR/sandbox_mirror.log" &
wait \$!
EOF

# ---- Mirror ③: 产物快照（只记变化）----
cat > "$MDIR/mirror_artifact.sh" <<EOF
#!/bin/bash
trap 'pkill -P \$\$ 2>/dev/null; exit 143' TERM INT
trap 'pkill -P \$\$ 2>/dev/null' EXIT
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
  # 同理：前台 sleep 期间 bash 不跑 trap（最坏要等满 45s 才响应 TERM）。后台+wait 即时响应。
  sleep 45 & wait \$!
done
EOF

chmod +x "$MDIR"/mirror_*.sh

# ★同 tag 重跑必须有分隔（复核 M3）★：清了 daemon，但三面 mirror 仍是 >> append，
# 旧轮内容与新轮混在同一文件、无任何分界——"复核者按行号回查对不上"在同 tag 重跑下原样复发。
_SESSION_BANNER="===== SESSION $(date '+%Y-%m-%d %H:%M:%S') tag=${TAG} task=${TID} source=${MIRROR_SRC} ====="
for _f in swarm sandbox artifact; do
  echo "$_SESSION_BANNER" >> "$MDIR/${_f}_mirror.log"
done

# 启动并落 pid 台账。**不能用 `P1=$(_start_daemon …)` 命令替换**——那会让整个函数体跑在
# 子 shell 里，`$!` 与追加台账都发生在子 shell，父 shell 拿不到、也无从 disown。
_start_daemon() {   # $1=脚本名；结果写全局 _LAST_PID
  nohup bash "$MDIR/$1" </dev/null >/dev/null 2>&1 &
  _LAST_PID=$!
  disown 2>/dev/null || true
  echo "$_LAST_PID" >> "$_PID_FILE"
}
_start_daemon mirror_swarm.sh;    P1=$_LAST_PID
_start_daemon mirror_sandbox.sh;  P2=$_LAST_PID
_start_daemon mirror_artifact.sh; P3=$_LAST_PID

echo "[e2e-mirrors] ✓ 三面镜像已常驻 tag=$TAG task=${TID:0:8} 来源=$MIRROR_SRC"
echo "  ①swarm    pid=$P1 → $MDIR/swarm_mirror.log"
echo "  ②sandbox  pid=$P2 → $MDIR/sandbox_mirror.log"
echo "  ③artifact pid=$P3 → $MDIR/artifact_mirror.log"
echo "  随跑日志: $JOURNAL"
echo "[e2e-mirrors] 下一步: 另起哨兵(run_in_background)按状态跃迁/高信号词唤醒。"
echo "  停止（trap 会带走 tail/grep 子进程）: while read p; do kill -TERM \$p; done < $_PID_FILE"
echo "  ⚠️ 别用 pkill -f ${TAG}_mirrors：只杀得掉 wrapper，tail/grep 会变孤儿继续灌串，且会误杀哨兵"
