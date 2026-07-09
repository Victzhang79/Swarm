---
id: springboot-security
title: Spring Boot 安全最佳实践
applies_to_stacks: ["java"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["java", "spring-boot", "security"]
---

加认证、处理输入、建端点、碰密钥时启用。核心：默认拒绝、校验输入、最小权限、安全默认配置。

认证
- 优先无状态 JWT / 不透明令牌（配吊销名单）；会话 Cookie 设 `httpOnly` `Secure` `SameSite=Strict`。
- 令牌校验在 `OncePerRequestFilter` 或资源服务器统一做；校验有效期与签名。

授权
- 开启方法级安全 `@EnableMethodSecurity`，敏感方法加 `@PreAuthorize("hasRole('ADMIN')")` 或 `@PreAuthorize("@authz.isOwner(#id, authentication)")`。
- 默认拒绝，只放开必要 scope；每条敏感路径都要有守卫。

输入校验
- DTO 上加约束（`@NotBlank` `@Email` `@Size` `@Min/@Max`），控制器入参加 `@Valid`。
- 渲染前对 HTML 做白名单净化。

SQL 注入
- 用 Spring Data 派生查询或参数化 `:param` 绑定；native 查询绝不字符串拼接。

口令与密钥
- 口令用 BCrypt/Argon2 经 `PasswordEncoder` 哈希，禁明文、禁手写哈希。
- 源码不含密钥；配置文件用占位符 `${DB_PASSWORD}`，从环境/保险库注入；定期轮换。

CSRF / CORS / 头
- 浏览器会话应用保留 CSRF；纯 Bearer API 关 CSRF 并走 `STATELESS`。
- CORS 在安全过滤链统一配，生产禁 `*`，显式列白名单来源。
- 设安全响应头：CSP `default-src 'self'`、frameOptions sameOrigin、Referrer-Policy。

限流与日志
- 贵端点加限流（如 Bucket4j / 网关），超限返回 429 并告警。
- 日志绝不含密钥/令牌/口令/完整卡号等敏感字段，脱敏后用结构化 JSON。

其他
- 文件上传校验大小/类型/扩展名，存 web 根目录外。
- CI 跑依赖漏洞扫描，命中已知 CVE 就阻断构建，保持框架在受支持版本。
