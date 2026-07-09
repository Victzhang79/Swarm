---
id: postgres-patterns
title: PostgreSQL 查询/索引/Schema 最佳实践
description: "当你在为 PostgreSQL 选索引（B-tree/GIN/BRIN/部分/覆盖索引）、选字段类型（timestamptz/text/numeric）、写 ON CONFLICT 上插或游标分页、排查慢查询时调用，返回索引选型表与高价值写法。"
applies_to_stacks: ["postgres"]  # G6：画像文本探出 postgres 才挂（互斥，探不出都不挂）
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["database", "sql", "postgres"]
---

# PostgreSQL 最佳实践速查

## 索引选型
| 查询模式 | 索引类型 |
|---|---|
| `col = v` / `col > v` | B-tree（默认） |
| `a = x AND b > y` | 复合索引 `(a, b)`：等值列在前、范围列在后 |
| `jsonb @> '{}'` / 全文 `@@` | GIN |
| 时序范围 | BRIN |

- 覆盖索引：`CREATE INDEX ix ON users(email) INCLUDE (name, created_at)` 免回表。
- 部分索引：`... WHERE deleted_at IS NULL` 只索引活跃行，更小更快。
- 外键列必须建索引（否则父表删改会全表扫）。

## 数据类型
| 用途 | 用 | 别用 |
|---|---|---|
| ID | `bigint` | `int`、随机 UUID |
| 字符串 | `text` | `varchar(255)` |
| 时间 | `timestamptz` | `timestamp` |
| 金额 | `numeric(10,2)` | `float` |
| 标志 | `boolean` | `int`/`varchar` |

## 高价值写法
- UPSERT：`INSERT ... ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v`。
- 游标分页：`WHERE id > $last ORDER BY id LIMIT n`（O(1)），别用大 OFFSET（O(n)）。
- 队列取任务：子查询 `FOR UPDATE SKIP LOCKED` 免锁竞争抢单。
- 行级安全策略里把子查询包一层 `(SELECT fn())` 让规划器只算一次。

## 排查
- 未加索引的外键、`pg_stat_statements` 里 `mean_exec_time` 高的慢查询、`pg_stat_user_tables.n_dead_tup` 大的膨胀表——三板斧定位。
- 配置底线：设 `statement_timeout` 与 `idle_in_transaction_session_timeout` 防长事务挂死；生产开 `pg_stat_statements`；`REVOKE ALL ON SCHEMA public FROM public` 收敛默认权限。

原则：为读写路径建对索引、类型选对、查询只取所需列、事务短小。
