---
id: jpa-patterns
title: JPA/Hibernate 持久化模式
description: "当你在写 JPA/Hibernate 实体与 Repository、治理 N+1（LAZY 关联+JOIN FETCH/DTO 投影）、配 @Transactional 事务或 Flyway 迁移时调用，返回持久化模式速查。"
applies_to_stacks: ["java"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["java", "jpa", "orm", "mapper", "repository", "entity", "dao", "mybatis", "persistence"]
---

# JPA/Hibernate 持久化模式

## 实体设计
- `@Table(indexes=@Index(...))` 在实体上声明索引；主键 `@GeneratedValue(strategy=IDENTITY)`。
- 枚举用 `@Enumerated(EnumType.STRING)`（禁 ORDINAL，改序会错位）。
- 审计字段用 `@CreatedDate`/`@LastModifiedDate` + 配置类加 `@EnableJpaAuditing`。

## 关系与 N+1 防治（重点）
- 关联默认 LAZY，绝不在集合上用 `EAGER`。
- 需要一次取全时在 JPQL 用 `JOIN FETCH`：
```java
@Query("select m from M m left join fetch m.children where m.id=:id")
```
- 读路径优先用 DTO/接口投影，只取需要的列，避免加载整实体。
```java
interface Summary { Long getId(); String getName(); }
Page<Summary> findAllBy(Pageable p);
```

## Repository / 分页
- 继承 `JpaRepository<E, ID>`；派生查询 `findBySlug`；分页返回 `Page<>` 传 `Pageable`。
- 分页：`PageRequest.of(n, size, Sort.by("createdAt").descending())`；游标式在 JPQL 加 `id > :lastId` + 排序。

## 事务
- Service 方法加 `@Transactional`；只读路径加 `@Transactional(readOnly=true)`。
- 事务短小，避免长事务持锁；谨慎选传播级别。

## 性能
- 为常用过滤列（状态、slug、外键）建索引；复合索引匹配查询顺序。
- 批量写用 `saveAll` + `hibernate.jdbc.batch_size`；避免 `select *`。
- HikariCP：`maximum-pool-size`/`minimum-idle`/`connection-timeout` 按负载调。

## 迁移与测试
- 用 Flyway/Liquibase，生产禁 Hibernate 自动 DDL；迁移幂等、增量。
- 用 `@DataJpaTest` + Testcontainers 贴近生产；开 `hibernate.SQL=DEBUG` 验证 SQL 效率。

要义：实体精简、查询有意图、事务短、按读写路径建索引。
