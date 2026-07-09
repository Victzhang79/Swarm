---
id: api-design
title: API 设计要点（栈无关）
description: "当你在设计对外 REST 接口、定 HTTP 状态码与错误体结构、做分页过滤参数或接口版本化兼容演进时调用，返回资源命名、幂等键与乐观锁等设计规则清单。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["plan", "code"]
target: ["worker", "planner"]
priority: 55
max_chars: 1000
tags: ["api", "rest", "design"]
---
设计/实现对外接口时：
- 资源命名：名词复数、层级清晰；用 HTTP 动词表达操作（GET 读、POST 建、PUT/PATCH 改、DELETE 删），别把动作塞进路径。
- 状态码：成功 2xx（201 表新建）、客户端错 4xx（400 校验、401 未认证、403 越权、404 不存在、409 冲突）、服务端错 5xx；语义准确。
- 错误体统一：稳定的机器可读结构（code + message + 可选 details），别只返回裸字符串。
- 入参校验：在边界一次性校验并给出清晰错误；不信任客户端。
- 分页/过滤/排序：列表接口提供有界分页，别一次返回全量。
- 幂等与并发：写接口考虑重复提交（幂等键）与并发覆盖（版本/乐观锁）。
- 兼容演进：加字段而非改语义；破坏性变更走版本化，不静默改契约。
- 与既有约定一致：沿用本项目已有的路由/响应/鉴权风格，不另起一套。
