---
id: django-patterns
title: Django 模式（模型/视图/ORM/DRF）
applies_to_stacks: ["python"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["python", "django", "orm"]
---

# Django 生产级模式

## 工程结构
- settings 拆分 base/development/production/test；密钥/DB 全走环境变量，`DEBUG=False` 为生产默认。
- 业务按 app 分目录，每 app 含 models/views/serializers/urls/services/tests。

## 模型
- 显式 `Meta`：`db_table`、`ordering`、`indexes`、`constraints`（如 `CheckConstraint(Q(price__gte=0))`）。
- 金额用 `DecimalField(max_digits, decimal_places)` 别用 float；加 validators。
- 高频过滤/排序字段建 `Index`；覆盖 `save()` 补 slug 等派生字段。

## 查询（防 N+1）
- 自定义 `QuerySet` 封装可复用查询：`.active()`/`.in_stock()`，`objects = MyQuerySet.as_manager()`。
- 外键用 `select_related('category')`，多对多用 `prefetch_related('tags')`。
- 批量走 `bulk_create`/`bulk_update`/`filter().update()`，别循环单条写库。

## DRF
- Serializer 做校验：`validate_<field>` 单字段、`validate(self,data)` 跨字段；只读字段进 `read_only_fields`。
- 按 action 切 serializer：`get_serializer_class()`；创建时 `perform_create(serializer.save(created_by=...))`。
- ViewSet 挂 `filter_backends`(DjangoFilterBackend+Search+Ordering)；额外端点用 `@action(detail=...)`。

## 分层与其它
- 复杂业务写进 service 层（静态方法 + `@transaction.atomic` 保证多表一致）。
- 缓存昂贵查询：`cache.get/set(key, val, timeout)`，读优先。
- 信号在 `AppConfig.ready()` 里导入注册；`post_save` 建关联对象（如用户建 Profile）。
