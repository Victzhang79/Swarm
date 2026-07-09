---
id: nestjs-patterns
title: NestJS 模式（模块/依赖注入/守卫）
applies_to_stacks: ["node"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 46
max_chars: 1800
tags: ["node", "nestjs", "backend"]
---

# NestJS 模式

分层：领域代码放各 feature module(`modules/xxx/`)；横切 filters/guards/interceptors/pipes 放 `common/`；DTO 紧邻其所属模块；config 集中在 `config/`。

- **Bootstrap 全局校验**：一个全局 `ValidationPipe({ whitelist: true, forbidNonWhitelisted: true, transform: true })`，公开 API 必开 whitelist；`ClassSerializerInterceptor` + 全局异常过滤器。别在每个路由重复校验配置。
- **模块/控制器/服务**：`@Module({ controllers, providers, exports })` 只 export 别的模块真正需要的 provider。**控制器保持薄**：解析 HTTP 入参→调 service→返回响应 DTO；业务逻辑放 `@Injectable()` 服务，不放控制器。参数用管道校验如 `@Param('id', ParseUUIDPipe)`。
- **DTO 与校验**：每个请求 DTO 用 `class-validator`(`@IsEmail`/`@IsString`/`@Length`/`@IsOptional`/`@IsEnum`)。**用专门响应 DTO/序列化器，绝不直接返回 ORM 实体**，避免泄漏 password hash、token、审计列。
- **认证/守卫/上下文**：`@UseGuards(JwtAuthGuard, RolesGuard)` + `@Roles('admin')`。守卫编码粗粒度访问规则，资源级授权放 service。认证请求对象用显式类型。
- **异常过滤器**：全 API 统一错误信封（`{ path, error }`）。预期客户端错误抛框架异常；未知错误集中 log 并包装成 500。
- **配置**：`ConfigModule.forRoot({ isGlobal, load, validate })` **启动时校验 env**（非首请求懒校验）；config 访问走类型化 service；dev/staging/prod 差异放 config 工厂而非散落分支。
- **持久化/事务**：ORM/Repository 藏在讲领域语言的 provider 后；事务工作单元由 service 拥有，控制器不协调多步写。

## 生产默认
结构化日志 + 请求关联 id；env 非法即终止启动不半启；DB/缓存客户端异步初始化 + 健康检查；后台任务/事件消费者独立模块；公开端点显式限流/鉴权/审计。

## 测试
`Test.createTestingModule` 编译；请求级测试覆盖守卫/校验管道/异常过滤器；测试复用与生产相同的全局 pipes/filters。
