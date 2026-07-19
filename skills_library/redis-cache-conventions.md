---
id: redis-cache-conventions
title: 缓存抽象正确用法——先探真实缓存类，CacheUtils/RedisCache/RedisTemplate 方法级 API（Java）
description: "当你在 Java 单体里读写缓存/会话/token/验证码，需要 setCacheObject/getCacheObject、CacheUtils、或注入 RedisTemplate 存取键值时调用。核心：不同项目缓存抽象不同（EhCache 的 CacheUtils / 包装器 RedisCache / 裸 RedisTemplate），先探项目真实存在的那一个再用；返回三种的正确方法签名 + '绝不臆造/引入项目里不存在的缓存抽象'幻觉黑名单。"
applies_to_stacks: ["java"]
applies_to_intents: ["create", "modify", "debug"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 3600
tags: ["redis", "rediscache", "cache", "cacheutils", "redistemplate"]
---

## 铁律 0：缓存抽象因项目而异——先 grep 项目真实存在的那一个，绝不假设

同是 Java/若依风格项目，缓存抽象**三选一且互斥**，用错一个就 `cannot find symbol` / `package does not exist` 死循环：

- **经典 Shiro + EhCache 单体**：只有 `com.ruoyi.common.utils.CacheUtils`（EhCache 支撑），**没有 Redis、没有 RedisCache、没有 RedisTemplate**。
- **Redis 包装器变体**：有 `com.ruoyi.common.core.redis.RedisCache`（包装 RedisTemplate，方法名 `xxxCacheObject`）。
- **直接用 spring-data-redis**：注入原生 `RedisTemplate`，走 `opsForValue()`。

**写任何缓存代码前，先 `grep -rl 'RedisCache\|CacheUtils\|RedisTemplate' src` 定位项目到底用哪个**，再照抄同仓最近一处调用点（如 `SysLoginService`/`TokenService`/`SysUserOnlineServiceImpl`）的类型与方法。**探到哪个用哪个；探不到 Redis 就绝不引入它。**

## ★头号幻觉（round65e8 真死因）★ 别把不存在的缓存抽象塞进项目

经典 Shiro+EhCache 基线**根本没有 Redis**。worker 凭"新版若依有 Redis"的训练惯性，硬注入 `RedisTemplate redisCache` 并调 `redisCache.set(k,v,ttl,unit)` → 类/依赖都不存在，`package does not exist`，换几个模型都编不过。**判据：pom 里没有 `spring-boot-starter-data-redis`、源码 grep 不到 `RedisTemplate`/`RedisCache` → 这个项目就是 EhCache 缓存，用 `CacheUtils`，绝不引入 Redis 依赖或类。**

## CacheUtils（EhCache 单体的真实 API）

```java
import com.ruoyi.common.utils.CacheUtils;
Object v = CacheUtils.get(cacheName, key);        // 命名缓存 + key（也有单参 get(key)）
CacheUtils.put(cacheName, key, value);
CacheUtils.remove(cacheName, key);
CacheUtils.removeAll(cacheName);
```

## RedisCache 包装器（仅当项目确有该类时）

`RedisCache` 方法名一律 `xxxCacheObject`，**不是** `set/get`：

```java
@Autowired private RedisCache redisCache;
redisCache.setCacheObject(key, value);
redisCache.setCacheObject(key, value, 300, TimeUnit.SECONDS);
MyType v = redisCache.getCacheObject(key);   // 返回目标类型（泛型推断）
redisCache.deleteObject(key);
redisCache.expire(key, 300, TimeUnit.SECONDS);
redisCache.setCacheList(key, list);  List<T> l = redisCache.getCacheList(key);
```

**这些是幻觉，绝不写**（把包装器当裸 RedisTemplate/JdkMap）：`redisCache.set(k,v)`、`redisCache.set(k,v,ttl,unit)`、`redisCache.get(k)`、`redisCache.get(k,Class)`、`redisCache.delete(k)`、`redisCache.put(k,v)`、`redisCache.opsForValue()...`。

## 原生 RedisTemplate（仅当同仓样板确实直接这么用）

```java
redisTemplate.opsForValue().set(k, v, 300, TimeUnit.SECONDS);
Object v = redisTemplate.opsForValue().get(k);
```
**但别把它命名成 `redisCache` 再调 `setCacheObject`**——那是把两套不通用的 API 混用。

## 与 infra 符号硬约束的关系

栈画像的"基建符号·硬约束"给你**项目真实存在的类 FQN**（类级）；本技能补**方法签名 + 别引入不存在的抽象**（方法级 + 边界）。类不存在（幻觉引入）或方法名写错，都是同一类 `cannot find symbol` 死循环。
