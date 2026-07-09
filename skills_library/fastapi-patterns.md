---
id: fastapi-patterns
title: FastAPI 模式（依赖注入/Pydantic/异步）
description: "当你在写 FastAPI 路由、Pydantic v2 schema、Annotated Depends 依赖注入、async 事务边界或 httpx AsyncClient 测试时调用，返回分层模式与反例对照。"
applies_to_stacks: ["python"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["python", "fastapi", "api"]
---

# FastAPI 模式

分层：`routers/`(HTTP 薄层) · `schemas/`(Pydantic) · `services/`(业务+事务) · `models/`(ORM) · `dependencies.py`(共享依赖)。

- **App 工厂 + lifespan**：`create_app()` 返回实例；`@asynccontextmanager` lifespan 里启动建连、`yield` 后 `await engine.dispose()` 释放池。中间件(CORS)在工厂内注册。
- **配置**：`pydantic-settings` 的 `BaseSettings` + `SettingsConfigDict(env_file=".env")`，从环境读密钥/URL，禁止硬编码。
- **Pydantic v2 schema**：请求/响应分离。`Field(min_length=…)` 校验；`@model_validator(mode="after")` 做跨字段校验（如两次密码一致）；响应类 `model_config={"from_attributes": True}`。**路由必须声明 `response_model`**，防泄漏内部字段并生成干净 OpenAPI。
- **依赖注入**：用类型别名收敛样板 `DbDep = Annotated[AsyncSession, Depends(get_db)]`。`get_db` 用 async 生成器，异常时 `await session.rollback()` 再 raise。**认证与授权分层**：`get_current_user`(401) 与 `get_current_active_user`(403) 分开，给出精确状态码。JWT 解码防御式：捕获 `JWTError/TypeError/ValueError` 并做 str→int 转换。
- **路由薄、service 厚**：处理器只解析入参→调 service→返回。业务与事务边界放 service。
- **事务/并发**：唯一性依赖 DB 约束(`unique=True`)而非应用层预检（防竞态）；`commit()` 捕获 `IntegrityError` → rollback + 抛领域异常。分页强制确定性排序 `.order_by(Model.id)` 再 offset/limit，避免漏行。

## 反模式
- 处理器内直接写业务/建 ORM/commit → 应下沉 service。
- async 路由里用同步 DB 调用（`db.query(...)`）阻塞事件循环 → 用 `await db.execute(select(...))`。

## 测试
httpx `AsyncClient(transport=ASGITransport(app=app))` + pytest-asyncio；用 `app.dependency_overrides[get_db]` 注入内存 SQLite（`sqlite+aiosqlite:///:memory:`）；fixture 链 registered_user→auth_token→auth_client。
