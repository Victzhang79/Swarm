---
id: redis-patterns
title: Redis 缓存/数据结构模式
description: "当你在用 Redis 做缓存（Cache-Aside/TTL/击穿防惊群）、INCR 或滑动窗口限流、SET NX 分布式锁、Streams 消费组队列时调用，返回数据结构选型表与反模式清单（如生产禁 KEYS *）。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["redis", "cache"]
---

# Redis 模式速查

## 结构选型
| 用途 | 结构 |
|---|---|
| 简单缓存 | String (`GET/SETEX`) |
| 会话 | Hash |
| 排行榜 | Sorted Set |
| 唯一集合 | Set |
| 计数/限流 | String (`INCR`) |
| 近似去重计数 | HyperLogLog |
| 持久队列 | Stream |

## 缓存
- Cache-Aside：先读缓存，miss 则查库回填 `setex(key, ttl, json)`；读多、容忍轻微陈旧
- Write-Through：先写库再更新缓存；强一致
- 失效：按 tag 分组（`sadd` 关联键到 tag set），批量 `delete`
- **务必给每个 key 设 TTL**，无 TTL 的 key 会无界堆积

## 限流
- 固定窗口：`INCR` + `EXPIRE`（用 pipeline 事务），低频够用
- 滑动窗口：Lua 脚本内 `ZREMRANGEBYSCORE` 清旧 + `ZCARD` 计数 + `ZADD`，保证原子且精确

## 分布式锁
```
SET lock:res <token> NX PX <ttl_ms>   # 获取
# 释放用 Lua 校验 token 再 del（避免误删他人锁）
if get(KEYS[1])==ARGV[1] then return del(KEYS[1]) end
```
- 必在 `finally` 释放；多节点用 Redlock

## 消息
- Pub/Sub：fire-and-forget，无投递保证
- Streams：需 at-least-once/消费组/重放时用，`xadd(maxlen=N)` 限长 + `xreadgroup`/`xack`

## 连接与运维
- 用连接池（设 `max_connections`、`socket_timeout`），HA 用 Sentinel/Cluster
- 淘汰策略：一般缓存 `allkeys-lru`；关键数据/队列 `noeviction`

## 反模式
- 生产禁 `KEYS *`（O(N) 阻塞）→ 用 `SCAN`
- 禁存 >100KB 大 blob → 存引用
- 缓存击穿：冷启动加锁/概率提前过期防惊群
