---
id: laravel-patterns
title: Laravel 架构模式
description: "当你在写 Laravel（PHP）的 Controller/Service/Action 分层、Eloquent with 预加载防 N+1、Form Request 校验、队列任务与缓存失效时调用，返回架构分层规则与代码范例。"
applies_to_stacks: ["php"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["php", "laravel", "mvc"]
---

生产级 Laravel 架构。分层清晰：Controller → Service/Action → Model；IO 密集入队列，贵读走缓存，配置集中在 `config/*`。

分层
- 控制器保持瘦，只做接线；编排放 Service，单一用例逻辑放 Action。
- 接口在 ServiceProvider 里 `bind` 到实现，依赖显式装配。
```php
final class OrdersController extends Controller {
    public function __construct(private CreateOrderAction $create) {}
    public function store(StoreOrderRequest $r): JsonResponse {
        return response()->json(['data' => OrderResource::make(
            $this->create->handle($r->toDto()))], 201);
    }
}
```

路由
- 优先资源控制器 + 路由模型绑定；嵌套用 `scopeBindings()` 强制父子归属、防跨租户越权。
- 前缀与参数名和绑定模型一致（`{conversation}` 对 `Conversation`），避免双重嵌套。

Eloquent
- 用 `$fillable`、`$casts`（枚举/值对象）、命名 scope 收敛领域逻辑。
- 防 N+1：查询用 `with([...])` 预加载。
- 复杂过滤抽 Query 对象；同一过滤别同时用全局 scope 和命名 scope（除非有意分层）。
- 多步写用 `DB::transaction`；可恢复记录用 `SoftDeletes`。

校验
- 校验放 Form Request，`authorize()` 里做授权，`rules()` 里定规则，再 `toDto()` 转 DTO。

迁移
- 文件名带时间戳、用匿名类；表名 snake_case 复数；外键用 `constrained()->cascadeOnDelete()`。

API 响应
- 统一用 API Resource + 分页，响应结构保持一致（data / meta 带分页信息）。

队列 / 缓存 / 配置
- 领域副作用（邮件、分析）发事件；慢活（报表、导出、webhook）入队列，处理器幂等 + 重试退避。
- 读多端点与贵查询做缓存，模型事件时失效，关联数据用 tag 便于批量失效。
- 密钥放 `.env`，配置放 `config/*.php`，生产 `config:cache`。
