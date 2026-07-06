#!/usr/bin/env bash
# e2e_env_check.sh —— E2E 起跑前【环境自检】（可反复沿用）。
#
# 为什么要有它：`nc -z` 在部分 macOS 上不可靠（曾把 4 个实际 UP 的服务全误报 down）；
# 且 Redis 有个隐蔽陷阱——`SWARM_DB_REDIS_URI` 在 .env 里≠启用，真正开关是 `SWARM_REDIS_ENABLED`。
# 本脚本用【权威探针】（psql / redis-cli / curl /healthz），并专门检出
# 「SWARM_REDIS_ENABLED=true 但 Redis 没在跑」这类会白跑一轮的错配。
#
# 用法：bash scripts/e2e_env_check.sh   （在 swarm/ 包目录跑；退出码 0=全绿可开跑，非 0=有硬缺口）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

GREEN='\033[0;32m'; RED='\033[0;31m'; YEL='\033[0;33m'; NC='\033[0m'
ok(){ echo -e "  ${GREEN}✓${NC} $1"; }
bad(){ echo -e "  ${RED}✗${NC} $1"; FAIL=1; }
warn(){ echo -e "  ${YEL}!${NC} $1"; }
FAIL=0

# ── 读 .env（权威取 URI / 开关；不 export 敏感值到日志）──
[ -f .env ] && set -a && . ./.env 2>/dev/null && set +a
PG_URI="${SWARM_DB_POSTGRES_URI:-postgresql://localhost:5432/swarm}"
REDIS_URI="${SWARM_DB_REDIS_URI:-redis://localhost:6379/0}"
QDRANT_URL="${SWARM_DB_QDRANT_URL:-http://localhost:6333}"
SANDBOX_URL="${SWARM_SANDBOX_API_URL:-}"
REDIS_ON="$(echo "${SWARM_REDIS_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
REQ_PG_CKPT="$(echo "${SWARM_REQUIRE_PG_CHECKPOINTER:-}" | tr '[:upper:]' '[:lower:]')"
API_PORT="${SWARM_PORT:-${SWARM_API_PORT:-8420}}"  # R2-5：与 restart-api.sh 的 SWARM_PORT 对齐（旧名保兼容）
REDIS_CLI="$(command -v redis-cli || echo /opt/homebrew/opt/redis/bin/redis-cli)"

echo "== E2E 环境自检 =="

# ── PG（必需）──
if psql "$PG_URI" -tAc "select 1" >/dev/null 2>&1; then
  TBLS=$(psql "$PG_URI" -tAc "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null)
  ok "PostgreSQL 可达（public 表 ${TBLS:-?} 张）"
  [ "${TBLS:-0}" -lt 3 ] 2>/dev/null && warn "表数偏少，可能未建表：跑 python scripts/init_db.py"
else
  bad "PostgreSQL 不可达（$PG_URI）—— 必需，起 brew services start postgresql@16"
fi

# ── Redis（按开关判定；专检错配）──
REDIS_UP=0
"$REDIS_CLI" -u "$REDIS_URI" ping 2>/dev/null | grep -qi PONG && REDIS_UP=1
if [ "$REDIS_ON" = "true" ] || [ "$REDIS_ON" = "1" ] || [ "$REDIS_ON" = "yes" ]; then
  if [ "$REDIS_UP" = 1 ]; then
    ok "Redis 已启用且在跑（真·目标拓扑：跨进程 ModuleLock / 队列自愈 / renew 墙钟闸 全生效）"
  else
    bad "★错配★ SWARM_REDIS_ENABLED=true 但 Redis 没在跑 —— 会 fail-open 静默降级、白跑 Redis 路径。起 brew services start redis"
  fi
else
  if [ "$REDIS_UP" = 1 ]; then
    warn "Redis 在跑但【未启用】(SWARM_REDIS_ENABLED 未设为 true) → 代码走 in-memory 降级，不验 Redis 硬化路径"
  else
    warn "Redis 未启用也未在跑 → in-memory 单进程降级（可跑，但不覆盖 v0.9.9 Redis 依赖硬化）"
  fi
fi

# ── Qdrant（KB 语义；缺失降级非硬失败）──
if curl -sf -m3 "$QDRANT_URL/healthz" >/dev/null 2>&1; then
  NC_=$(curl -sf -m3 "$QDRANT_URL/collections" 2>/dev/null | grep -oE '"name"' | wc -l | tr -d ' ')
  ok "Qdrant 可达（${NC_:-?} 个 collection）"
else
  warn "Qdrant 不可达（$QDRANT_URL）→ KB 语义检索降级（结构化 KB 仍可用，非硬失败）"
fi

# ── Sandbox（worker 执行；root 常返 404=服务在）──
if [ -n "$SANDBOX_URL" ]; then
  scode=$(curl -s -m5 -o /dev/null -w "%{http_code}" "$SANDBOX_URL" 2>/dev/null || echo 000)
  if [ "$scode" != "000" ]; then
    ok "Sandbox 可达（$SANDBOX_URL -> HTTP ${scode}, worker 沙箱执行依赖它）"
  else
    bad "Sandbox 不可达（$SANDBOX_URL）—— worker 无法执行，确认沙箱机 IP/端口"
  fi
else
  warn "SWARM_SANDBOX_API_URL 未设 → worker 走本机执行（须自备目标栈工具链）"
fi

# ── Checkpointer 拓扑提示 ──
if [ "$REQ_PG_CKPT" = "1" ] || [ "$REQ_PG_CKPT" = "true" ]; then
  ok "PG checkpointer 强制（SWARM_REQUIRE_PG_CHECKPOINTER）→ 崩溃恢复/人工闸 resume 可验"
else
  warn "PG checkpointer 未强制 → 本机默认 MemorySaver（dev），重启不保中断态（如需验崩溃恢复设 =1）"
fi

# ── API 就绪（若在跑）──
if curl -sf -m3 "http://127.0.0.1:${API_PORT}/api/health" >/dev/null 2>&1; then
  ver=$(curl -s -m3 "http://127.0.0.1:${API_PORT}/api/health" | grep -oE '"version":"[^"]*"' | cut -d'"' -f4)
  rdy=$(curl -s -m3 "http://127.0.0.1:${API_PORT}/api/health/ready" | grep -oE '"status":"[^"]*"' | head -1 | cut -d'"' -f4)
  ok "API 在跑（v${ver:-?}，readiness=${rdy:-?}）"
else
  warn "API 未在跑（起跑序第 1 步：bash scripts/restart-api.sh，端口 ${API_PORT}）"
fi

echo "=================="
if [ "$FAIL" = 0 ]; then
  echo -e "${GREEN}环境自检通过 —— 可进 E2E 起跑序（soak → 三清 → reset → fresh submit → 三盯）${NC}"; exit 0
else
  echo -e "${RED}有硬缺口（见上 ✗）—— 修好再开跑，别白跑一轮${NC}"; exit 1
fi
