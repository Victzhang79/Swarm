#!/usr/bin/env bash
# 在 CubeSandbox 集群侧（能 docker build + cubemastercli 的节点）执行：
#   构建 5 语言依赖预热镜像 → push 本地 registry → create-from-image 建 exec(2c2g)+verify(4c4g)
#   两套模板 → watch 到 READY → 打印 10 个 template_id（回填 Swarm sandbox_templates 配置）。
#
# ★2026-07-06 更新（CubeSandbox 0.5.0 适配，与 worker/image_builder.py 同治本）★：
#   - REGISTRY 改本地 registry（localhost:5000）：0.5.0 的 create-from-image 只从 registry 拉、
#     不再读本地 docker 镜像；本地 registry 由 swarm-registry 容器提供，节点内闭环不出网。
#   - docker build 加 --provenance=false：出单 v2s2 manifest（buildkit 默认 OCI index 被 registry 拒）。
#   - create-from-image 加 --node（钉单节点，防多节点 rootfs 竞态）+ --with-cube-ca=true
#     （0.4.0+ CubeEgress MITM，沙箱须信任根 CA 否则 HTTPS 出网全断）+ --allow-internet-access。
#   - exec/verify 双变体：exec 默认 2c2g（写代码轻量），verify --cpu 4000 --memory 4000（4c4g，重编译）。
set -euo pipefail

# ─── 配置（可用环境变量覆盖）───────────────────────────────
REGISTRY="${REGISTRY:-localhost:5000}"          # 本地 registry（swarm-registry 容器）
REGISTRY_IMAGE="${REGISTRY_IMAGE:-ccr.ccs.tencentyun.com/library/registry:2}"  # 自启用
NODE="${NODE:?必须指定 --node 钉的健康节点 IP（= swarm SSH 的那台），如 NODE=10.0.0.x}"  # 环境变量必传，勿硬编码入库
TAG="${TAG:-v2}"
WRITABLE_LAYER="${WRITABLE_LAYER:-2G}"
# ──────────────────────────────────────────────────────

HERE="$(cd "$(dirname "$0")" && pwd)"
DF_DIR="$HERE/dockerfiles"
LANGS=(python node java go rust)

# D45：普通索引数组（按 LANGS 下标对齐）——declare -A 需 bash4+，macOS 自带 bash3.2 直接语法崩；
# 索引数组 bash3.2+ 通用，行为等价。
EXEC_TPL=()
VERIFY_TPL=()
log() { echo -e "\033[36m[cube-tpl]\033[0m $*"; }

# 0) 按需自启本地 registry（幂等）
if [[ "$REGISTRY" == localhost:* || "$REGISTRY" == 127.0.0.1:* ]]; then
  port="${REGISTRY##*:}"
  docker inspect swarm-registry >/dev/null 2>&1 || \
    docker run -d -p "${port}:5000" --restart=always --name swarm-registry "$REGISTRY_IMAGE" >/dev/null 2>&1 || true
  curl -sS -m5 -o /dev/null -w '[cube-tpl] 本地 registry /v2 = %{http_code}\n' "http://${REGISTRY}/v2/" || true
fi

# create-from-image 公共参数（0.4.0+ 必带）
COMMON_OPTS=(--node "$NODE" --with-cube-ca=true --allow-internet-access
             --writable-layer-size "$WRITABLE_LAYER" --expose-port 49983 --probe 49983 --probe-path /health)

# 提交 create-from-image，回显 template_id 与 job_id（v0.5.0 输出 job_id=xxx）
create_tpl() {  # $1=image  $2..=额外参数(如 --cpu/--memory)
  local img="$1"; shift
  local out; out="$(cubemastercli template create-from-image --image "$img" "${COMMON_OPTS[@]}" "$@" 2>&1)"
  echo "$out" >&2
  local tpl job; tpl="$(echo "$out" | grep -oE 'tpl-[0-9a-f]+' | head -1 || true)"
  job="$(echo "$out" | grep -oE 'job_id[:=][[:space:]]*[0-9a-f-]+' | head -1 | grep -oE '[0-9a-f-]{8,}' || true)"
  # D45：tpl 为空（create 输出解析不到 template_id）→ 直接判失败返回。
  # 旧码继续 `template list | grep "$tpl"`——grep 空模式匹配【所有行】，撞上任意 READY
  # 模板即误报成功、输出空 template_id（下游拿空串回填配置，静默坏）。
  if [[ -z "$tpl" ]]; then echo "<建失败:no-template-id>"; return 0; fi
  # watch 到 READY（有 job_id 用 watch，否则轮询 tpl list）
  if [[ -n "$job" ]]; then
    cubemastercli template watch --job-id "$job" >&2 2>&1 || true
  fi
  local st; st="$(cubemastercli template list 2>/dev/null | grep "$tpl" | awk '{print $2}' | head -1)"
  if [[ "$st" == "READY" ]]; then echo "$tpl"; else echo "<${tpl}:status=${st:-?}>"; fi
}

for i in "${!LANGS[@]}"; do
  lang="${LANGS[$i]}"
  img="$REGISTRY/sandbox-$lang:$TAG"
  log "==== [$lang] docker build --provenance=false → $img ===="
  docker build --provenance=false -f "$DF_DIR/Dockerfile.$lang" -t "$img" "$DF_DIR"

  # envd /health 自测（防发布坏模板，见 NODE_ENVD_FIX.md）
  # D45：cid 为空（docker run 失败/entrypoint 秒崩）是【最坏信号】，必须视为 health FAIL
  # 拒发——旧码此时整体跳过 /health 直接 push+create，坏镜像照发（fail-open）。
  cid="$(docker run -d -P "$img" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    log "❌ [$lang] 容器起不来（docker run 失败），视为 health FAIL，拒发"
    EXEC_TPL[$i]="<envd自测失败:容器未启动>"; VERIFY_TPL[$i]="<envd自测失败:容器未启动>"; continue
  fi
  sleep 5
  hp="$(docker port "$cid" 49983/tcp 2>/dev/null | head -1 | cut -d: -f2)"
  if curl -fsS -m5 "localhost:${hp}/health" >/dev/null 2>&1; then log "✅ [$lang] envd /health 通过"
  else log "❌ [$lang] envd /health 不通，跳过"; docker rm -f "$cid" >/dev/null 2>&1 || true
       EXEC_TPL[$i]="<envd自测失败>"; VERIFY_TPL[$i]="<envd自测失败>"; continue; fi
  docker rm -f "$cid" >/dev/null 2>&1 || true

  log "[$lang] push → $img"
  docker push "$img"

  log "[$lang] create exec 模板 (2c2g) ..."
  EXEC_TPL[$i]="$(create_tpl "$img")"
  log "[$lang] create verify 模板 (4c4g) ..."
  VERIFY_TPL[$i]="$(create_tpl "$img" --cpu 4000 --memory 4000)"
  log "[$lang] exec=${EXEC_TPL[$i]}  verify=${VERIFY_TPL[$i]}"
done

echo ""
log "================  完成。以下为新模板（exec / verify）================"
for i in "${!LANGS[@]}"; do
  echo "RESULT ${LANGS[$i]} exec=${EXEC_TPL[$i]} verify=${VERIFY_TPL[$i]}"
done
