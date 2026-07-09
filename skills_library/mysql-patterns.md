---
id: mysql-patterns
title: MySQL 查询/索引/Schema 最佳实践
description: "当你在设计 MySQL/MariaDB 表结构、复合索引、EXPLAIN 调优、ON DUPLICATE KEY 上插、keyset 分页或处理 InnoDB 死锁时调用，返回 Schema/索引/事务规则与反模式清单。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["database", "sql", "mysql"]
---

# MySQL/MariaDB 模式速查

先确认引擎与版本（`SELECT VERSION()`）；MySQL 与 MariaDB 语法有分歧，按版本套用。

## Schema 默认
- 代理主键 `BIGINT UNSIGNED AUTO_INCREMENT`（大表别用 INT）
- 金额/精确量用 `DECIMAL(p,s)`，禁 `FLOAT/DOUBLE`
- 文本用 `utf8mb4`（非 `utf8/utf8mb3`）
- 时间用 `DATETIME`（应用侧管 UTC；DATETIME 不存时区）
- 软删 `deleted_at DATETIME NULL` + 配套局部索引
- 引擎 `InnoDB`；`created_at/updated_at` 用 `DEFAULT CURRENT_TIMESTAMP [ON UPDATE ...]`

## 索引
- 复合索引顺序：等值谓词在前，范围/排序列在后
- 改索引前先 `EXPLAIN`，警惕：`type=ALL`、`key=NULL`、`rows` 过大、`Extra` 出现 `Using filesort/temporary`
- 别盲目加索引，每个索引增加写/迁移/备份成本

## 查询
- Upsert 跨引擎：`INSERT ... ON DUPLICATE KEY UPDATE col=VALUES(col)`（MariaDB/混合）；确认 MySQL 才用行别名 `AS new ... new.col`
- 分页用 keyset：`WHERE (created_at,id) < (?,?) ORDER BY ... LIMIT n`，配对索引；**禁深 OFFSET**
- JSON 仅存扩展数据；高频查询路径用 generated column + 索引，关系/租户/生命周期字段保持关系化

## 事务
- 保持短事务；锁行按确定顺序（`ORDER BY id FOR UPDATE`）防死锁
- 外部 API 调用放事务外
- 死锁 → 回滚并有界重试；事后 `SHOW ENGINE INNODB STATUS`
- 队列领取用 `FOR UPDATE SKIP LOCKED`（仅队列型，非一致性读）

## 连接池
- `pool_recycle` 低于服务器 `wait_timeout`，开 `pool_pre_ping` 抗失效/failover

## 安全 & 反模式
- 运行账号最小权限，禁 `ALL PRIVILEGES`/`*.*`，跨网要求 TLS，凭据入密钥管理器
- 禁热路径 `SELECT *`；FK 列建索引；读写后避免立即读从库（复制延迟）
