---
id: cost-aware-llm-pipeline
title: LLM 成本优化与模型路由
description: "当你在搭建 LLM 调用管线、需要按复杂度路由便宜/强模型、设预算上限 fail-fast、只对限流 5xx 窄重试或做提示缓存时调用，返回四件套代码骨架与反模式清单。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["plan", "code"]
target: ["worker", "planner"]
priority: 40
max_chars: 1800
tags: ["llm", "cost", "pipeline"]
---

# LLM 成本优化与模型路由

控成本又不牺牲难任务质量：模型路由 + 预算闸 + 窄重试 + 提示缓存，四件套可组合。

## 1. 按复杂度路由模型
简单任务走便宜模型，复杂任务才升级到强模型；模型名用常量/配置，别散落硬编码。
```python
def select_model(text_len, item_count, force=None):
    if force: return force
    if text_len >= 10_000 or item_count >= 30:
        return STRONG_MODEL      # 复杂
    return CHEAP_MODEL           # 简单，通常便宜数倍
```
阈值要记录选择决策，用真实数据回调。

## 2. 预算追踪（不可变）
用 frozen dataclass 累计花费，每次调用返回新 tracker、绝不原地改（便于审计与回放）。批处理前设死预算上限，超限即 fail-fast，别边跑边超支。
```python
@property
def over_budget(self): return self.total_cost > self.budget_limit
```

## 3. 窄重试
只对瞬时错误（网络中断、限流、5xx 服务端错误）指数退避重试；鉴权错误、参数错误立即抛出——重试它们只会白烧预算。
```python
RETRYABLE = (ConnectionError, RateLimitError, ServerError)
# backoff: time.sleep(2 ** attempt)，MAX_RETRIES=3
```

## 4. 提示缓存
超过约 1024 token 的系统提示做缓存，避免每次重发——省成本也省延迟。把稳定的系统提示与可变的用户输入分块，只在稳定块打缓存标记。

## 反模式
- 所有请求都用最贵模型，不看复杂度。
- 对全部错误重试（在永久失败上浪费预算）。
- 原地改花费状态（难调试、难审计）。
- 重复系统提示不做缓存。
