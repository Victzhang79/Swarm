---
id: maven-dependency-management
title: Maven 依赖管理（坐标·版本·BOM·scope）
description: "当你在 pom.xml 里增删依赖、决定要不要写 <version>、导入 BOM、处理 reactor 内部模块依赖或排除传递依赖时调用，返回坐标与版本的判定规则。"
applies_to_stacks: ["java", "kotlin"]
applies_to_intents: ["create", "modify", "debug"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 44
max_chars: 2400
tags: ["maven", "pom", "dependency", "bom", "version", "scope", "reactor", "artifact"]
---

在 pom.xml 里写依赖，第一位的问题永远是：**这条依赖的版本从哪来**。写错的代价不对称——版本缺失会让 Maven 在 **POM 解析期**就失败，整个 reactor 一个模块都读不出来；而少一条依赖只是本模块编译报错，可归因、可修。

## 决定 `<version>` 写不写：只有三种合法情况

1. **父级 dependencyManagement 管得到** → **不写** version（写死会覆盖工程统一版本）。
   管得到包括父 pom 显式声明的，以及父 pom **import 进来的 BOM**（如 `spring-boot-dependencies`）传递管理的坐标。
2. **父级管不到的第三方** → **必须写显式 version**。
   ```xml
   <dependency>
       <groupId>cn.hutool</groupId>
       <artifactId>hutool-all</artifactId>
       <version>5.8.47</version>   <!-- BOM 不管它 → 不写就是解析期硬错 -->
   </dependency>
   ```
3. **reactor 内部兄弟模块** → `<version>${project.version}</version>`（与父同版），且该模块必须真实存在于根 pom 的 `<modules>` 里。

判断"父级管不管得到"的唯一可靠方法是查父 pom 的 `<dependencyManagement>` 和它 import 的 BOM，**不要凭印象**。`mvn help:effective-pom` 能看到解析后的最终版本。

## 绝不能做的两件事

- **绝不臆造坐标**：不确定 groupId 就不要写这条依赖。把第三方 artifact 挂到工程自己的 groupId 下（`com.yourcorp:spring-boot-starter-web`）会让整棵树解析失败。
- **绝不依赖不存在的模块**：只有出现在根 pom `<modules>` 里、且真有对应目录/pom 的模块才能被依赖。依赖一个"计划中但没人创建"的模块 = 幻影坐标，同样是解析期硬错。

## BOM 导入（集中管版本）

```xml
<dependencyManagement>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-dependencies</artifactId>
            <version>${spring-boot.version}</version>
            <type>pom</type>
            <scope>import</scope>
        </dependency>
    </dependencies>
</dependencyManagement>
```
BOM 之后，被它管辖的依赖在子模块里**不写版本**即可。

## scope 速查

| scope | 编译期 | 测试期 | 运行期 | 传递 |
|---|---|---|---|---|
| compile（默认） | ✓ | ✓ | ✓ | ✓ |
| provided | ✓ | ✓ | ✗ | ✗ |
| runtime | ✗ | ✓ | ✓ | ✓ |
| test | ✗ | ✓ | ✗ | ✗ |

驱动、日志实现用 `runtime`；Servlet API、Lombok 这类容器/编译期提供的用 `provided`；测试库一律 `test`。

## 版本选择

- 只用**稳定版**。`4.0.0-M2`、`3.0.0-alpha-1`、`7.1.0-RC1` 这类里程碑/预发布版常与工程基线不兼容，且很多镜像仓库没有——编译期"过了"，集成期照炸。
- 不要用版本区间和 `LATEST`/`RELEASE`：不可复现。
- 版本抽到 `<properties>` 里统一管理，同一 artifact 在多个模块里必须同版本。

## 排除传递依赖

```xml
<dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter</artifactId>
    <exclusions>
        <exclusion>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-logging</artifactId>
        </exclusion>
    </exclusions>
</dependency>
```
排除的理由要写注释。冲突诊断用 `mvn dependency:tree -Dverbose`（能看到"就近者胜"的裁决过程），无用依赖用 `mvn dependency:analyze`。
