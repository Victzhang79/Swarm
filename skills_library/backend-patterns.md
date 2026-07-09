---
id: backend-patterns
title: 后端架构与 API/数据/缓存模式（栈无关）
description: "当你在实现后端 Controller/Service/Repository 分层、资源式 URL、Cache-Aside 缓存、消灭 SQL N+1 或 Redis 限流与 RBAC 鉴权时调用，返回各环节模式与反例对照。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 55
max_chars: 1800
tags: ["backend", "architecture", "api"]
---

# 后端架构模式

## 分层
- Controller/Handler 只做请求解析+响应；业务逻辑归 Service；数据访问归 Repository（接口抽象，便于换存储/加缓存/测试）。
- 中间件流水线：鉴权、日志、限流用可组合的装饰器/wrapper，别把横切逻辑塞进 handler。

## API 设计
- 资源式 URL：`GET /items` 列表、`GET /items/:id` 单个、`POST/PUT/PATCH/DELETE`；过滤/排序/分页走 query：`?status=active&sort=-created&limit=20&offset=0`。
- 统一响应包：`{success, data}` / `{success:false, error, details?}`；HTTP 码语义正确（400 校验、401 未认证、403 无权、404、409 冲突、429 限流、5xx）。

## 数据库
- 只 select 需要的列，不用 `SELECT *`。
- 消灭 N+1：循环里查询 → 批量取 id 再一次查 + Map 关联。
- 多写操作放事务；跨行不变量在 DB 层（唯一约束/外键/CHECK），别只靠应用层判断。
- 热点查询建索引，命中过滤/排序列。

## 缓存（Cache-Aside）
- 先查缓存→未命中查库→回填并设 TTL；写/删数据时主动失效对应 key。
- key 带版本或实体前缀（`entity:id`）；TTL 按数据易变度取。

## 错误处理
- 集中式 error handler：按异常类型映射状态码；已知业务异常用自定义 Error 携带 statusCode；未知异常记日志+返回通用 500，不泄露内部细节。
- 外部调用用指数退避重试（1s,2s,4s，上限次数），仅对可重试错误重试。

## 鉴权
- token 校验失败即 401；RBAC 用「角色→权限集」表驱动，`hasPermission(user, perm)` 判定，缺权返回 403。

## 限流/队列/日志
- 限流用共享存储（Redis/网关），禁用单进程内存计数器（多副本/部署重启会失效、fail-open）。
- 重活入队异步处理，别阻塞请求线程。
- 结构化 JSON 日志，带 requestId/userId/method/path；错误带 message+stack，禁止记 PII/密钥。
