# Swarm 备份与恢复 Runbook（P2-G）

单进程 + PostgreSQL + Redis + Qdrant 拓扑下的数据安全操作手册。

## 数据资产与恢复优先级

| 资产 | 载体 | 丢失后果 | 可否重建 |
|------|------|----------|----------|
| 业务数据（项目/任务/审计/记忆/KB 符号） | **PostgreSQL** | 全部丢失 | 否（必须备份） |
| 图执行 checkpoint | PostgreSQL（LangGraph） | 中断任务无法 resume | 否（随 PG 一起备） |
| KB 向量索引 | **Qdrant** | 语义检索退化 | 是（可由 PG 源码 + 预处理重建，耗时） |
| 加密凭据（API keys 等） | PostgreSQL（密文）+ **SWARM_SECRET_KEY**（根密钥） | 根密钥丢=密文永不可解 | 否（根密钥必须托管） |
| 分布式锁/队列 | Redis | 崩溃恢复靠启动对账（P0-A），无需备份 | 是（进程重启 + reconcile 自愈） |

> **核心**：PostgreSQL 是唯一不可重建的真相源；**SWARM_SECRET_KEY 是解密一切的钥匙**，丢了 = DB 里的加密凭据全部作废。二者必须【分开】异地保存（备份与根密钥不放一处，否则拿到备份即全解）。

## 备份

```bash
bash scripts/backup.sh [输出目录]     # 默认 ./backups/<UTC 时间戳>
```

产出：`postgres.dump`（pg_dump custom 格式）+ `qdrant-<collection>.snapshot`（可缺，非致命）。
建议：每日 cron 跑一次并同步到异地对象存储；`SWARM_SECRET_KEY` 单独进 KMS/vault（**不入备份目录**）。

**RPO（可容忍数据丢失窗口）**：= 备份间隔。每日备份 → 最多丢 24h。要更小则缩短间隔或开 PG 持续归档（WAL archiving / PITR）。
**RTO（恢复耗时目标）**：单库逻辑恢复通常十分钟级（视库大小）；Qdrant 无快照时按项目重跑预处理，可能小时级。

## 恢复

前置：目标 PostgreSQL 已建库、pgvector 扩展可用；`SWARM_SECRET_KEY` 已从托管取回并设入环境。

```bash
# 1) PostgreSQL（--clean 先删同名对象再建；空库可去掉 --clean）
pg_restore --clean --if-exists --no-owner --no-privileges \
           --dbname "$SWARM_DB_POSTGRES_URI" backups/<TS>/postgres.dump

# 2) Qdrant（有快照时）——上传快照并恢复
curl -X POST "$SWARM_DB_QDRANT_URL/collections/<collection>/snapshots/upload" \
     -H 'Content-Type:multipart/form-data' \
     -F "snapshot=@backups/<TS>/qdrant-<collection>.snapshot"
#   无快照时：起服务后对各项目重跑预处理（WebUI/CLI 触发），由 PG 源码重建向量。

# 3) 校验：起 API，探 /api/health/ready 应 200（PG/Qdrant 就绪）；抽查任务/项目列表完整。
```

## 校验与演练

- 定期（如每季度）在隔离环境跑一次【完整恢复演练】，确认备份可用 + 记录实际 RTO。
- 恢复后务必核对 `SWARM_SECRET_KEY` 与备份时一致——不一致则 secret_store 密文无法解密（表现为读取 API key 报错）。
- `/api/health/ready` 返回 200 且能正常提交/审批一个任务，视为恢复成功。
