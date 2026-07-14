---
id: maven-build-lifecycle
title: Maven 构建生命周期与多模块 reactor
description: "当你要跑/调 Maven 构建、选择编译校验命令、处理多模块 reactor 顺序、用 -pl/-am 只编某几个模块，或排查构建失败在哪个阶段时调用。"
applies_to_stacks: ["java", "kotlin"]
applies_to_intents: ["create", "modify", "debug"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 42
max_chars: 2200
tags: ["maven", "build", "lifecycle", "reactor", "compile", "multi-module", "pom"]
---

Maven 的阶段是**有序累积**的：跑后面的阶段会把前面的都跑一遍。选对阶段能省大量时间。

## 阶段与常用命令

```
validate → compile → test-compile → test → package → verify → install → deploy
```

```bash
mvn -q compile              # 只编主源码（验证语法/符号，最快）
mvn -q test-compile         # 连测试代码一起编
mvn -q test                 # 跑单测
mvn -q package -DskipTests  # 打包但不跑测试
mvn clean install           # 清干净重装到本地仓库
```

自检产出时优先用 `compile`：它能抓住绝大多数错误（缺依赖、符号错、语法错），且不必等测试。

## 多模块 reactor：只编你改的那部分

```bash
mvn -pl module-a,module-b -am -q compile
```
- `-pl` 指定要构建的模块（逗号分隔）。
- `-am`（also make）连带构建它们**依赖的**上游模块。
- `-amd` 反向：连带构建依赖它们的下游。
- `-rf :module-x` 从某模块**续跑**（前面已成功的不重来）。

**只把自己真正改动的模块放进 `-pl`**。把不相干的模块圈进来，别人模块里的错误会连坐判死你的构建。

## reactor 的两条硬约束

1. 根 pom `<modules>` 里列出的每个模块，**目录和 pom.xml 必须真实存在**，否则整个 reactor 起不来（`Could not find the selected project in the reactor`）。
2. 任何一个模块 pom 解析失败（缺 `<version>`、坐标不存在、XML 非法、标签重复），**整棵树都读不出来**——此时报错位置在别的模块，不代表你的模块有问题。

## 看懂失败发生在哪一层

- `Some problems were encountered while processing the POMs` / `'dependencies.dependency.version' ... is missing` / `Non-parseable POM`
  → **POM 解析期**失败，还没开始编译。先修 pom，别去看 Java 代码。
- `Could not resolve dependencies` / `Could not find artifact`
  → 坐标或版本在仓库里不存在。核对 groupId/artifactId/version。
- `COMPILATION ERROR` / `cannot find symbol` / `package X does not exist`
  → 真正的编译错。`package does not exist` 通常意味着**依赖没声明**，不是代码写错。

## 排障命令

```bash
mvn -X ...                    # debug 输出
mvn help:effective-pom        # 看继承/BOM 解析后的最终 pom（版本到底是多少）
mvn dependency:tree -Dverbose # 依赖树与冲突裁决
```

## 纪律

- 构建命令要能复现：不要依赖本机 IDE，任何人 `mvn -pl <你的模块> -am -q compile` 都应通过。
- 插件版本必须钉死（见 maven-plugin-configuration），否则今天绿明天红。
- 不要用 `-Dmaven.test.skip=true` 掩盖测试编译错——那会把真问题藏起来。
