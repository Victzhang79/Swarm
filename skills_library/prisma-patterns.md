---
id: prisma-patterns
title: Prisma ORM 模式（Schema/查询/迁移）
applies_to_stacks: ["node"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 46
max_chars: 1800
tags: ["node", "prisma", "orm"]
---

# Prisma 模式

先 `npx prisma --version` 核对版本：新装可能包名/驱动适配器(`@prisma/adapter-pg`)/config 位置不同，让编译器告诉你构造函数是否需 `adapter`。

## Schema
- ID：`cuid()` 为默认(URL 安全/可排序)；`uuid()` 仅需跨系统互操作；`autoincrement()` 仅内部表(公开会暴露记录数)。
- 每个外键及 WHERE/ORDER BY 列都加 `@@index`；`@unique` 已自带索引。软删预留 `deletedAt DateTime?`（事后加要在活表上迁移）。
- `@updatedAt` 仅在 `update`/`upsert` 自动触发。

## 查询
- `select`(显式白名单，热路径/大表) vs `include`(全标量+关系)。**绝不直接返回原始实体**，映射到响应 DTO 控制暴露字段。
- N+1：循环里查关系 → 改单次 `include`。
- 事务选型：独立操作用数组式 `$transaction([...])`；后步依赖前步用交互式(只用 `tx` 客户端)；含外部调用(邮件/HTTP)放事务外。
- **PrismaClient 单例**：每实例开独立连接池，用 `globalThis` 缓存防热重载重复实例。
- 游标分页：取 `limit+1` 再 pop 判 `hasNextPage`；`orderBy` 必带唯一字段(如 id)作二级排序防抖动。
- 软删：显式 `where: { deletedAt: null }`，别靠中间件(隐藏行为难调)。
- 错误：捕获 `Prisma.PrismaClientKnownRequestError`，`P2002` 唯一冲突/`P2025` 未找到/`P2003` 外键，在 service 边界翻译成领域错误，不暴露原始消息。

## 反模式（高危）
- `updateMany`/`deleteMany` 返回 `{ count }` 非记录 → 先查 id 再更新再取。
- 交互式 `$transaction` 默认 5s 超时 → 外部调用移出事务，批处理才 `{ timeout: 30_000 }`。
- `migrate dev` 会因漂移重置库丢数据 → 共享/staging/prod 一律 `migrate deploy`，`migrate dev` 只本地。
- 手改已应用的迁移文件 → `P3006` 校验和不符，改为新建迁移。
- 破坏性变更(加 NOT NULL/改列名)用 expand-and-contract 三步。
- `@updatedAt` 不随 `updateMany` 触发 → bulk 写手动 `updatedAt: new Date()`。
- 软删 + `findUniqueOrThrow` 会漏出已删行且不支持非唯一 where → 用 `findFirstOrThrow`。
- `deleteMany()` 无 `where` 清空全表 → 必带 `where`。
