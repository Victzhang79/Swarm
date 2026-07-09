---
id: django-security
title: Django 安全最佳实践
description: "当你在配置 Django 生产安全项（DEBUG/ALLOWED_HOSTS/HSTS）、DRF 权限与限流、防 SQL 注入/XSS/CSRF 或校验文件上传时调用，返回逐项安全设置清单。"
applies_to_stacks: ["python"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["python", "django", "security"]
---

# Django 安全清单

## 生产设置（必查）
- `DEBUG=False`；`ALLOWED_HOSTS` 白名单；`SECRET_KEY` 只从环境变量取，缺失即抛异常拒启。
- 强制 HTTPS 与安全 Cookie：`SECURE_SSL_REDIRECT` / `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE` / `SECURE_HSTS_SECONDS=31536000`。
- 头部：`SECURE_CONTENT_TYPE_NOSNIFF=True`、`X_FRAME_OPTIONS='DENY'`、可加 CSP。
- Cookie 加 `HTTPONLY=True` + `SAMESITE='Lax'`。

## 认证/口令
- 自定义 User（`AUTH_USER_MODEL`），email 唯一。
- 开全部 `AUTH_PASSWORD_VALIDATORS`，`min_length>=12`；`PASSWORD_HASHERS` 首选 Argon2。

## 授权
- 视图用 `LoginRequiredMixin`+`PermissionRequiredMixin`，`raise_exception=True` 返回 403。
- `get_queryset()` 收窄到当前用户可见对象（对象级隔离）。
- DRF 自定义 `BasePermission.has_object_permission`：SAFE_METHODS 放行读，写只给 owner。

## 注入/XSS
- SQL：只用 ORM；必须 raw 时用参数化 `raw('... = %s', [val])`，绝不 f-string 拼用户输入。
- 模板默认转义；`|safe`/`mark_safe` 仅对可信内容，用户输入先 `escape()` 再 `format_html`。

## CSRF / 文件上传 / API
- CSRF 默认开启别关；非浏览器 webhook 才 `@csrf_exempt`（谨慎）。
- 上传校验：读 magic bytes 定 MIME（`filetype`/`python-magic`）并交叉核对扩展名 + 限大小。
- DRF 限流 `DEFAULT_THROTTLE_RATES`（anon/user/upload 分级）；默认 `IsAuthenticated`。

## 其它
- 密钥/DB URL 走 `.env`（不入库）；记录 `django.security` 日志；及时升级依赖。
