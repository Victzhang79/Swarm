#!/usr/bin/env bash
# E2E 跑前基线重置 —— 测试流程固定护栏。
#
# 目的：每轮 E2E 必须从【干净基线】开跑，杜绝上一轮的半成品/残留文件污染本轮产出与判断
# （实证 RUN6→RUN7：未重置 → RUN6 的 18 个残留文件混入 RUN7，st-2 误报悬空引用，
#  把"脏环境"误诊成"代码 bug"，差点据脏证据改 scope guard）。
#
# 安全红线：绝不盲目 `git clean -fd`（曾误删合法未跟踪的 ruoyi-ui）。本脚本：
#   1) git reset --hard HEAD —— 还原 tracked 改动 + 卸载暂存的新增模块
#   2) git clean -fd 但【排除】保留清单（缓存/IDE/构建产物/前端等合法未跟踪）
#   3) 删除前先 dry-run 审计打印将移除什么，可追溯
#
# 保留清单：默认 DEFAULT_KEEP；项目可在 <project>/.e2e-baseline-keep 追加（每行一个，# 注释）。
set -euo pipefail

PROJ="${1:?用法: e2e_reset_baseline.sh <project_path>}"
PROJ="$(cd "$PROJ" && pwd)"  # 规范化绝对路径

[[ -d "$PROJ/.git" ]] || { echo "[e2e-reset] ❌ $PROJ 不是 git 仓库，拒绝重置（防误删非版本控制目录）"; exit 1; }

# 合法未跟踪保留清单（这些不是上一轮产物，删了会破坏基线）：
#   .codegraph=代码图缓存  ruoyi-ui/前端=独立未跟踪源码  target/build/dist=构建产物  IDE/包缓存
DEFAULT_KEEP=(.codegraph ruoyi-ui ruoyi-ui-* node_modules target build dist out .gradle .idea .vscode .settings)
keep=("${DEFAULT_KEEP[@]}")
KEEP_FILE="${PROJ}/.e2e-baseline-keep"
if [[ -f "$KEEP_FILE" ]]; then
  while IFS= read -r line; do
    [[ -n "$line" && "$line" != \#* ]] && keep+=("$line")
  done < "$KEEP_FILE"
fi

cd "$PROJ"
echo "[e2e-reset] 项目: $PROJ"
echo "[e2e-reset] HEAD: $(git rev-parse --short HEAD) $(git log -1 --pretty=%s | cut -c1-50)"

# 1) 还原到【真实基线 commit】——不能简单 reset 到 HEAD：swarm auto_accept 会把上一轮交付
#    commit 成 "swarm task output [taskid]" 推走 HEAD，reset 到 HEAD 会把上轮交付当基线污染本轮
#    (RUN10→RUN11 实证)。故定位最近一个【非 swarm 交付】commit 作为基线。可用 .e2e-baseline-ref
#    固定覆盖。
BASE_REF="$(cat "${PROJ}/.e2e-baseline-ref" 2>/dev/null || true)"
if [[ -z "$BASE_REF" ]]; then
  BASE_REF="$(git log --invert-grep --grep='^swarm task output' --format='%H' -1 2>/dev/null)"
fi
BASE_REF="${BASE_REF:-HEAD}"
_dropped=$(git rev-list "${BASE_REF}..HEAD" --count 2>/dev/null || echo 0)
git reset --hard "$BASE_REF" >/dev/null
echo "[e2e-reset] ✓ reset 到真实基线 $(git rev-parse --short HEAD)（丢弃 ${_dropped} 个 swarm 交付 commit）"

# 2) 组装 clean 排除参数
excl=()
for k in "${keep[@]}"; do excl+=(-e "$k"); done

# 3) dry-run 审计
to_remove="$(git clean -fdn "${excl[@]}" || true)"
if [[ -n "$to_remove" ]]; then
  echo "[e2e-reset] 将移除以下未跟踪产物（保留: ${keep[*]}）:"
  echo "$to_remove" | sed 's/^/    /'
  # 4) 实删
  git clean -fd "${excl[@]}" >/dev/null
  echo "[e2e-reset] ✓ 已移除上一轮残留产物"
else
  echo "[e2e-reset] ✓ 无残留产物可移除"
fi

# 4.5) 幽灵模块壳清理（R65E14-T6/#45）：上一轮交付的新模块源码已被 reset+clean 卸载，
#      但 target/build 等在 KEEP 清单里 → 留下"只含构建产物的空模块壳"（如 ruoyi-alarm/
#      只剩 target/）。危害：contract_utils 的 (base/mod).is_dir() 棕地兜底会把幽灵壳误判
#      "既有基线模块"。判据（双条件，防误删 ruoyi-ui 类合法未跟踪源码）：
#        ① 目录内无任何 git tracked 文件；② 目录下【只有】构建产物类条目。
_GHOST_ONLY="target build dist out .gradle node_modules"
for d in */; do
  d="${d%/}"
  [[ -d "$d" ]] || continue
  # ① 有 tracked 文件 → 真基线模块，跳过
  [[ -z "$(git ls-files -- "$d" | head -1)" ]] || continue
  # ② 目录下存在任何非构建产物条目（如 ruoyi-ui 的真实源码）→ 跳过
  _has_real=""
  for entry in "$d"/* "$d"/.[!.]*; do
    [[ -e "$entry" ]] || continue
    _name="$(basename "$entry")"
    _is_ghost=""
    for g in $_GHOST_ONLY; do [[ "$_name" == "$g" ]] && _is_ghost=1 && break; done
    [[ -n "$_is_ghost" ]] || { _has_real=1; break; }
  done
  [[ -z "$_has_real" ]] || continue
  rm -rf "$d"
  echo "[e2e-reset] ✓ 已清除幽灵模块壳 $d/（无 tracked 源码、只含构建产物——上轮交付残留）"
done

# 5) 校验：只允许保留清单里的未跟踪项存在
leftover="$(git status --porcelain)"
if [[ -z "$leftover" ]]; then
  echo "[e2e-reset] ✅ 基线完全干净（git status 为空）"
else
  # 残留若全是保留项则 OK，否则告警
  bad="$(echo "$leftover" | grep -vE "($(IFS='|'; echo "${keep[*]}"))" || true)"
  if [[ -z "$bad" ]]; then
    echo "[e2e-reset] ✅ 基线干净（仅保留项: $(echo "$leftover" | tr '\n' ' '))"
  else
    echo "[e2e-reset] ⚠️ 仍有非预期残留，请人工核查："
    echo "$bad" | sed 's/^/    /'
    exit 2
  fi
fi

# 6) 知识层外科清理 + 重建（R65-T2）：磁盘基线干净≠知识层干净——失败轮 worker 产物经
#    增量回灌进 PG kb_*/Qdrant，跨轮堆叠幻影模块污染下一轮规划检索（round65 实锤）。
#    默认开启；SWARM_E2E_KEEP_KNOWLEDGE=1 显式跳过（跳过必大声，不静默）。
#    时序要求：重建走 API preprocess（CAS 防重入），故本步应在 restart-api 之后执行；
#    API 未起时脚本会 fail-loud（PREPROCESS_PENDING），宁可挡住起跑也不带脏/空知识开跑。
if [[ "${SWARM_E2E_KEEP_KNOWLEDGE:-0}" == "1" ]]; then
  echo "[e2e-reset] ⚠️ SWARM_E2E_KEEP_KNOWLEDGE=1 → 跳过知识层清理（上轮回灌知识将带入本轮！）"
else
  PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  echo "[e2e-reset] 知识层外科清理（kb_*+Qdrant，保 mem_* 经验层）+ preprocess 重建…"
  "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/e2e_purge_project_knowledge.py" "$PROJ" \
    || { echo "[e2e-reset] ❌ 知识层清理/重建失败——按 fail-loud 拒绝放行基线"; exit 3; }
  echo "[e2e-reset] ✅ 知识层已重置到磁盘基线"
fi
