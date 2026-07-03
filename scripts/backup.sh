#!/usr/bin/env bash
# Swarm 备份脚本（P2-G）——PostgreSQL + Qdrant + secret_store 根密钥托管提示。
#
# 用法：
#   bash scripts/backup.sh [输出目录]        # 默认 ./backups/<UTC 时间戳>
#
# 依赖：pg_dump（PostgreSQL 客户端）、curl、jq（可选，仅美化输出）。
# 环境变量（缺省见括号）：
#   SWARM_DB_POSTGRES_URI   PG 连接串（.env 同源；缺省 postgresql://localhost:5432/swarm）
#   SWARM_DB_QDRANT_URL     Qdrant 地址（缺省 http://localhost:6333）
#   SWARM_DB_QDRANT_COLLECTION  集合名（缺省 swarm_kb）
#   SWARM_SECRET_KEY        secret_store 根密钥（★必须单独异地托管，见文末★）
#
# 恢复步骤 + RTO/RPO 见 scripts/BACKUP_RESTORE.md。
set -euo pipefail

PG_URI="${SWARM_DB_POSTGRES_URI:-postgresql://localhost:5432/swarm}"
QDRANT_URL="${SWARM_DB_QDRANT_URL:-http://localhost:6333}"
QDRANT_COLL="${SWARM_DB_QDRANT_COLLECTION:-swarm_kb}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${1:-./backups/${TS}}"
mkdir -p "${OUT_DIR}"

echo "==> Swarm 备份 → ${OUT_DIR}"

# 1) PostgreSQL 全库逻辑备份（custom 格式，支持并行/选择性恢复）。
echo "[1/3] pg_dump ..."
pg_dump --format=custom --no-owner --no-privileges \
        --file="${OUT_DIR}/postgres.dump" "${PG_URI}"
echo "      → postgres.dump ($(du -h "${OUT_DIR}/postgres.dump" | cut -f1))"

# 2) Qdrant 集合快照（通过 snapshot API；服务须可达）。
echo "[2/3] Qdrant snapshot (${QDRANT_COLL}) ..."
SNAP_JSON="$(curl -sS -X POST "${QDRANT_URL}/collections/${QDRANT_COLL}/snapshots" || true)"
# 仅当返回体确含快照名（含 "result" 上下文）才下载；错误体无 name → 跳过。
SNAP_NAME="$(printf '%s' "${SNAP_JSON}" | sed -n 's/.*"name":"\([^"]*\)".*/\1/p' | head -1)"
SNAP_FILE="${OUT_DIR}/qdrant-${QDRANT_COLL}.snapshot"
if [ -n "${SNAP_NAME}" ]; then
  # 下载失败不得中断整脚本（set -e）——PG 备份已成，Qdrant 可由源码重建，非致命。
  curl -sS "${QDRANT_URL}/collections/${QDRANT_COLL}/snapshots/${SNAP_NAME}" \
       -o "${SNAP_FILE}" || true
  if [ -s "${SNAP_FILE}" ]; then
    echo "      → qdrant-${QDRANT_COLL}.snapshot ($(du -h "${SNAP_FILE}" | cut -f1))"
  else
    rm -f "${SNAP_FILE}"
    echo "      ⚠️  Qdrant 快照下载为空/失败；KB 向量可由 PG 源码 + 预处理重建，非致命。"
  fi
else
  echo "      ⚠️  未能创建 Qdrant 快照（服务不可达或集合不存在）；KB 向量可由 PG 源码 + 预处理重建，非致命。"
fi

# 3) secret_store 根密钥托管提示（★绝不写进备份目录★）。
echo "[3/3] secret_store 根密钥托管检查 ..."
if [ -n "${SWARM_SECRET_KEY:-}" ]; then
  echo "      SWARM_SECRET_KEY 已设置。★请将其【单独】保存到密钥托管（KMS / vault / 离线保险箱），"
  echo "      不要与本备份放一处——否则拿到备份即可解密所有 secret_store 加密凭据。★"
else
  echo "      ⚠️  未设置 SWARM_SECRET_KEY：根密钥从 DB 连接串派生（弱）。生产必须显式设置并异地托管。"
fi

echo "==> 完成。备份内容：$(ls -1 "${OUT_DIR}" | tr '\n' ' ')"
echo "    校验/恢复见 scripts/BACKUP_RESTORE.md"
