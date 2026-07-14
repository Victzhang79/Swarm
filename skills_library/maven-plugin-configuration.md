---
id: maven-plugin-configuration
title: Maven 插件配置（编译器·测试·打包·质量闸）
description: "当你要配置 maven-compiler/surefire/failsafe/jar/shade/spring-boot 等插件、绑定执行阶段、设置 annotationProcessorPaths 或加质量闸时调用。"
applies_to_stacks: ["java", "kotlin"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 40
max_chars: 2200
tags: ["maven", "plugin", "compiler", "surefire", "packaging", "jacoco", "build", "pom"]
---

插件是 Maven 真正干活的地方。三条铁律：**版本钉死、配置集中、绑对阶段**。

## 版本必须钉死

不写 `<version>` 的插件会随 Maven 版本漂移，今天绿明天红。多模块工程把版本与共享配置放进父 pom 的 `<pluginManagement>`，子模块只声明用哪个插件：

```xml
<build>
  <pluginManagement>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.12.1</version>
        <configuration><release>17</release></configuration>
      </plugin>
    </plugins>
  </pluginManagement>
  <plugins>
    <plugin>
      <groupId>org.apache.maven.plugins</groupId>
      <artifactId>maven-compiler-plugin</artifactId>   <!-- 版本/配置继承自上面 -->
    </plugin>
  </plugins>
</build>
```

## 编译器插件

```xml
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-compiler-plugin</artifactId>
  <version>3.12.1</version>
  <configuration>
    <release>17</release>            <!-- 优于 source/target 各写一遍 -->
    <encoding>UTF-8</encoding>
    <compilerArgs><arg>-parameters</arg></compilerArgs>
    <annotationProcessorPaths>       <!-- 用了 Lombok/MapStruct 必须在这里声明 -->
      <path>
        <groupId>org.projectlombok</groupId>
        <artifactId>lombok</artifactId>
        <version>1.18.30</version>
      </path>
    </annotationProcessorPaths>
  </configuration>
</plugin>
```
用了注解处理器却不配 `annotationProcessorPaths`，症状是"代码里明明有 getter，编译却报 cannot find symbol"。

## 测试：单测与集成测试分家

- **surefire** 跑单测（`**/*Test.java`），绑在 `test` 阶段。
- **failsafe** 跑集成测试（`**/*IT.java`），绑在 `integration-test`/`verify`，失败不会中断打包。

```xml
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-surefire-plugin</artifactId>
  <version>3.2.3</version>
  <configuration>
    <includes><include>**/*Test.java</include></includes>
    <excludes><exclude>**/*IT.java</exclude></excludes>
  </configuration>
</plugin>
```

## 打包

- 普通 jar：`maven-jar-plugin`（需要可执行则配 `<mainClass>`）。
- Spring Boot 可执行 jar：`spring-boot-maven-plugin` 的 `repackage` goal——**别再叠 shade/assembly**，会互相打架。
- 依赖打进一个 jar（非 Spring Boot）：`maven-shade-plugin`，注意用 `ServicesResourceTransformer` 合并 `META-INF/services`，否则 SPI 失效。

## 执行绑定

```xml
<executions>
  <execution>
    <id>attach-sources</id>          <!-- id 要有意义，便于覆盖/排查 -->
    <phase>package</phase>
    <goals><goal>jar-no-fork</goal></goals>
  </execution>
</executions>
```
绑错阶段的典型症状：goal 根本没跑，或同一 goal 跑了两遍。

## 常见坑

1. 插件没写版本 → 构建不可复现。
2. 用了 Lombok 不配 annotationProcessorPaths → 满屏 cannot find symbol。
3. Spring Boot 项目又加 shade → 双重打包，启动失败。
4. 在 CI 里 `-DskipTests` 把测试跳过 → 质量闸形同虚设。
5. 子模块重复声明父 pom 已管的插件配置 → 配置分叉，改一处不生效。
