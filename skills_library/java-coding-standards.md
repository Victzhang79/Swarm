---
id: java-coding-standards
title: Java 编码规范
applies_to_stacks: ["java"]
applies_to_intents: ["*"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1800
tags: ["java", "style", "best-practice"]
---

面向 Java 17+ 服务的可读、可维护规范。先按构建文件判定框架（Spring / Quarkus / 纯 Java），再套用对应约定。

命名
- 类/记录 PascalCase；方法/字段 camelCase；常量 UPPER_SNAKE_CASE。
- REST 入口命名与框架一致：Spring 用 `*Controller`，JAX-RS 用 `*Resource`。

不可变优先
- 默认 `final` 字段 + 只读 getter；DTO 用 `record`。
- 少共享可变状态；避免静态可变全局态，改用依赖注入。

依赖注入
- 一律构造器注入，不要字段注入（`@Autowired`/`@Inject` 打字段）。
- 需代理/拦截的 CDI bean 用 `@ApplicationScoped`，勿用 `@Singleton`。

Optional / 流
```java
return repo.findBySlug(slug)          // find* 返回 Optional
    .map(Resp::from)
    .orElseThrow(() -> new NotFoundException(slug)); // 不要 .get()
```
- 流用于转换，管线保持短；复杂嵌套流改回循环更清晰。

异常
- 领域错误用非受检异常，技术异常包裹补充上下文；定义领域专属异常类。
- 集中处理：Spring 用 `@RestControllerAdvice`，JAX-RS 用 `ExceptionMapper`。
- 禁止空 catch；要么记录并处理，要么重抛。

其他要点
- 泛型不用裸类型，工具方法用有界泛型。
- 日志用参数化占位（`log.info("fetch id={}", id)`），不字符串拼接。
- 入参用 Bean Validation（`@NotNull`/`@NotBlank`/`@Valid`）。
- 配置走类型安全绑定（`@ConfigurationProperties` / `@ConfigMapping`），不散读。
- 消除坏味道：长参数列表→DTO/builder；深嵌套→提前返回；魔法数→具名常量。
- 测试 JUnit5 + AssertJ + Mockito；确定性，无隐藏 sleep；控制器/仓储用切片测试。

原则：意图清晰、类型明确、可观测；优先可维护性，非必要不做微优化。
