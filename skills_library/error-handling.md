---
id: error-handling
title: 错误处理与健壮性模式（栈无关）
description: "当你在设计异常分层（AppError 带 code/status）、统一错误响应信封、做指数退避加抖动重试或排查静默吞错时调用，返回错误处理规则与自查 Checklist。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify", "debug"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 55
max_chars: 1800
tags: ["error-handling", "robustness"]
---

# 错误处理与健壮性模式

## 核心原则
- fail fast：在错误发生的边界立即暴露，别掩埋
- 用结构化 typed error（带 `code`/`status`）而非裸字符串
- 用户消息 ≠ 开发者消息：对用户显示友好文案，服务端记录完整上下文
- 绝不静默吞错：每个 `catch`/`except` 必须处理、重抛或记日志三选一
- 错误码是 API 契约的一部分，需文档化

## 错误类型分层
- 建 `AppError` 基类携带 `code` + `status_code`，派生 `NotFoundError`(404)/`ValidationError`(422)/`UnauthorizedError`(401)/`RateLimitError`(429)
- 预期且高频的失败（解析、外部调用）用 Result/Either 风格返回 `{ok, value|error}`，避免异常控制流

## 统一错误响应
- 全局 handler：已知 `AppError` → 按 `code`/`status` 输出统一信封 `{error:{code,message,details?}}`；未知异常 → 记完整栈，只回 500 通用文案
- 校验错误聚合 `field+message` 列表

## 重试（指数退避 + 抖动）
```
delay = min(base * 2**(attempt-1) + random_jitter, max_delay)
```
- 仅重试可重试错误：网络/5xx/超时；**不重试** 4xx 客户端错误
- 设 `max_attempts`（如 3）与 `max_delay` 上界；超限抛最后一次错误
- 幂等性存疑的写操作重试前需带幂等键

## Checklist
- [ ] 无静默吞错
- [ ] 错误走统一信封，用户文案无栈/内部细节
- [ ] 服务端记录完整上下文
- [ ] 自定义错误继承基类且带 `code`
- [ ] async/后台任务的错误上抛给调用方，无 fire-and-forget 无兜底
- [ ] 重试只针对可重试错误且有次数/退避上界
