#!/usr/bin/env bash
# 在 CubeSandbox 集群侧（能 docker build 且 registry 集群可达的机器）执行：
#   构建 5 个依赖预热镜像 → 推送 → cubemastercli tpl create-from-image → 打印新 template_id
#
# 前置：
#   - docker 可用；已 docker login 到 REGISTRY
#   - cubemastercli 可用且已指向你的 Cube Master
#   - 网络能拉 ghcr.io/tencentcloud/cubesandbox-base（或先手动 pull 到本地/私服）
set -euo pipefail

# ─── 改这里 ───────────────────────────────────────────
REGISTRY="${REGISTRY:-your-registry.example.com/swarm}"   # 集群可达的镜像仓库前缀
TAG="${TAG:-v1}"
WRITABLE_LAYER="${WRITABLE_LAYER:-2G}"   # 沙箱可写层大小（编译产物用，Java 建议≥2G）
# ──────────────────────────────────────────────────────

HERE="$(cd "$(dirname "$0")" && pwd)"
DF_DIR="$HERE/dockerfiles"
LANGS=(python node java go rust)

declare -A NEW_TPL

log() { echo -e "\033[36m[cube-tpl]\033[0m $*"; }

for lang in "${LANGS[@]}"; do
  img="$REGISTRY/sandbox-$lang:$TAG"
  log "==== 构建 $lang → $img ===="
  docker build -f "$DF_DIR/Dockerfile.$lang" -t "$img" "$DF_DIR"

  # 构建后自测：envd /health（防 node 那种 envd 故障发布坏模板）
  log "envd 健康自测 $lang ..."
  cid="$(docker run -d -p 49983:49983 "$img" 2>/dev/null || true)"
  if [ -n "$cid" ]; then
    sleep 5
    if curl -fsS localhost:49983/health >/dev/null 2>&1; then
      log "✅ $lang envd /health 通过"
    else
      log "❌ $lang envd /health 不通 —— 镜像 envd 故障，跳过 create-from-image！（见 NODE_ENVD_FIX.md）"
      docker rm -f "$cid" >/dev/null 2>&1 || true
      NEW_TPL[$lang]="<envd自测失败,未创建模板>"
      continue
    fi
    docker rm -f "$cid" >/dev/null 2>&1 || true
  else
    log "⚠️ $lang 无法本地启动自测（端口占用?），跳过自测继续"
  fi

  log "推送 $img"
  docker push "$img"

  log "创建模板 (create-from-image) ..."
  # 输出含 template_id，抓出来
  out="$(cubemastercli tpl create-from-image \
        --image "$img" \
        --writable-layer-size "$WRITABLE_LAYER" \
        --expose-port 49983 \
        --probe 49983 \
        --probe-path /health 2>&1)"
  echo "$out"
  tpl="$(echo "$out" | grep -oE 'tpl-[0-9a-f]+' | head -1 || true)"
  NEW_TPL[$lang]="${tpl:-<解析失败,看上面输出>}"
done

echo ""
log "================  完成。回填 Swarm 的 .env  ================"
echo "SWARM_SANDBOX_TEMPLATE_PYTHON=${NEW_TPL[python]}"
echo "SWARM_SANDBOX_TEMPLATE_NODE=${NEW_TPL[node]}"
echo "SWARM_SANDBOX_TEMPLATE_JAVA=${NEW_TPL[java]}"
echo "SWARM_SANDBOX_TEMPLATE_GO=${NEW_TPL[go]}"
echo "SWARM_SANDBOX_TEMPLATE_RUST=${NEW_TPL[rust]}"
log "填入后重启 Swarm API：SWARM_SANDBOX_POOL_ENABLED=true bash scripts/restart-api.sh"
