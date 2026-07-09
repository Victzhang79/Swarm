---
id: springboot-patterns
title: Spring Boot 惯用写法与最佳实践
description: "当你在写 Spring Boot 分层代码（Controller/Service/Repository、构造器注入、@Transactional 事务边界、jakarta/javax 包名一致性、DTO 与 JPA 实体分离）时调用，返回惯用规则清单。"
applies_to_stacks: ["java"]
applies_to_intents: ["*"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 50
max_chars: 1100
tags: ["java", "spring-boot", "best-practice"]
---
- 分层清晰：Controller 只做入参校验与编排、Service 承载业务、Repository 只做数据访问；别在 Controller 写业务或直接连库。
- 依赖注入用构造器注入（final 字段），不用字段注入；便于测试与不可变。
- 命名空间随项目：Servlet/JPA/校验注解的包名（jakarta.* 或 javax.*）必须与本项目现有代码一致，绝不混用——classpath 没有的包会直接编译失败。
- 事务边界放在 Service 方法上（`@Transactional`）；只读查询标 readOnly；别把事务开在 Controller。
- DTO 与实体分离：对外用 DTO，别直接暴露 JPA 实体；用校验注解在边界校验。
- 异常统一处理：用 `@ControllerAdvice` 全局处理，返回统一错误体，不在各处散落 try-catch 吞异常。
- 配置外置：用 `@ConfigurationProperties`/`application.yml`，不硬编码；密钥走环境变量。
- 沿用项目既有基类/工具类与鉴权变体（Shiro 或 Spring Security），不臆造 classpath 中不存在的类。
