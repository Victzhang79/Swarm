#!/usr/bin/env python3
"""B-0 共享**非 Maven workspace 夹具**（27 号文 §7 B-0：红灯先行的硬前提）。

## 为什么要共享夹具（不是洁癖）

27 号文 §4.3 实测：非 Maven 侧**零回归网**——提到任一非 Maven 栈的测试文件仅 74/718，
6244 个测试函数里真正多栈 parametrize 的只有 7 块，**没有一个测栈分发路由本身**；
非 Maven 的工程树全是各文件现场造的 1~5 文件微型树。结论原文：

> 把 `pom.xml` 特判改成通用清单表、或调整栈优先级，大概率一条测试都不会变红。

那种微型树的问题不是"小"，是**形状不真**：`_reconcile_gradle` 只认根直接子目录、
`_reconcile_cargo` 只扫根+一层、`_safe_subdirs` 跳 `_SKIP_DIRS`——现场造的两文件树
恰好绕开了这些前提句，于是测试**走不进**被测分支（"非 git 目录测 git archive 路径"
那类假绿的同族）。本模块把四型真实拓扑一次造对，供多个消费者复用。

## 四型（+2）

| builder | topology | 聚合清单 | 造它为了照出什么 |
|---|---|---|---|
| `npm_workspaces` | workspace | `package.json` `workspaces` | 唯一**两档都无网**的已收录栈（无 `_reconcile_npm`、无模块脚手架 driver） |
| `go_work` | workspace | `go.work` `use(...)` | R-1 主犯之一：曾判死却无人收敛 |
| `cargo_workspace` | workspace | `Cargo.toml` `[workspace] members` | 同上；且 glob 成员覆盖语义 |
| `gradle_kts` | workspace | `settings.gradle.kts` | `.kts` **别名**档（F-1 实测别名整列落空的现场） |
| `go_single_root` | single-root | 无 | L5 血泪：别拿 Maven reactor 当所有栈的模块观——单根仓**没有**per-module 清单 |
| `maven_reactor` | reactor | `pom.xml` `<modules>` | **对照臂**：同一断言在 Maven 上必须也成立（防"只为异栈放宽"） |

`go_single_root` 与 `maven_reactor` 超出 B-0 原列的"四型"：前者是 topology 维度的第三档
（无聚合清单的栈，`aggregate_manifest=None` 的行为只有它能测），后者是对照臂——两个都不是
新栈，只是同一批夹具里缺了会让矩阵测不出东西的格子。

## 夹具铁律

1. **每型都留一个"磁盘有、聚合清单未登记"的模块**（`unregistered`）——这是
   `_reconcile_*` 的唯一入口条件，也是 D1 双写者/demote 的现场。
2. **每型都埋噪声目录**（`node_modules` / `target` / `build` / `vendor` / `dist`）——
   `source_exclude_dirs` 与 `_SKIP_DIRS` 若失效，矩阵会当场红。
3. **绝不迁就现状**：夹具写的是**正确期望**；已知缺口在消费者测试里用
   `xfail(strict=True)` 标注（修好那天变 XPASS → 失败 → 逼人回来摘标记，不留僵尸豁免）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceFixture:
    """一棵造好的工程树 + 它的**机读事实**（消费者据此断言，不许再手抄一份路径）。"""

    name: str
    """builder 名（= parametrize 的 id）。"""

    root: Path
    """工程根（tmp_path 下的真实目录）。"""

    stack: str
    """栈键，必须是 `swarm.stacks.STACK_SPEC` 的键（由 test 对账，防夹具与事实表漂移）。"""

    lang: str
    """语言一等字段（java|node|go|rust|python）。"""

    aggregate_manifest: str | None
    """根聚合清单相对路径。None = 该栈无聚合登记机制（single-root 拓扑）。"""

    registered: tuple[str, ...] = field(default_factory=tuple)
    """聚合清单里**已登记**的模块目录（相对根）。"""

    unregistered: tuple[str, ...] = field(default_factory=tuple)
    """磁盘上有、聚合清单里**缺**的模块目录 —— `_reconcile_*` 该补的正是它们。"""

    module_manifests: tuple[str, ...] = field(default_factory=tuple)
    """每模块清单相对路径（single-root 栈为空元组）。"""

    sources: tuple[str, ...] = field(default_factory=tuple)
    """参与编译的源码相对路径（`is_compilable_source` 必须全判 True）。"""

    noise: tuple[str, ...] = field(default_factory=tuple)
    """**不**参与编译的文件（vendored / 产物 / 纯声明）——必须全判 False。"""

    topology: str = "workspace"
    """reactor | workspace | single-root（L5：模块观不可跨栈外推）。"""


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
# npm workspaces —— 唯一两档都无网的已收录栈
# ══════════════════════════════════════════════════════════════════

def build_npm_workspaces(root: Path) -> WorkspaceFixture:
    """`packages/*` 布局；根 `workspaces` 只登记了 core，web 在磁盘上但未登记。

    ★npm 的 `unregistered` 是**真缺口**★ 无 `_reconcile_npm`（spec
    `has_aggregate_reconcile=False`）→ 没人会补 web，`npm ci` 装不到它（X-H3）。
    夹具如实造出这个形状，别替实现圆场。
    """
    # ★根**无** scripts、子包**有** build★ 这是 workspaces 的常见形态（根只做编排，
    # 构建脚本在各包；用 turbo/nx 的仓根才有 scripts）。形状必须是这个——`_infer_npm`
    # 只读根的 scripts，所以只有"根无、子包有"才照得出 N-1；若连子包也没 scripts，
    # 那测的是另一回事（"纯静态资源不装 node"的启发式本身），区分力弱得多。
    _w(root, "package.json",
       '{\n  "name": "root",\n  "private": true,\n'
       '  "workspaces": ["packages/core"]\n}\n')
    _w(root, "packages/core/package.json",
       '{\n  "name": "@demo/core",\n  "version": "1.0.0",\n  "main": "src/index.ts",\n'
       '  "scripts": {"build": "tsc -b"}\n}\n')
    _w(root, "packages/web/package.json",
       '{\n  "name": "@demo/web",\n  "version": "1.0.0",\n  "main": "src/app.ts",\n'
       '  "scripts": {"build": "tsc -b", "test": "vitest run"}\n}\n')
    _w(root, "packages/core/src/index.ts", "export const core = 1;\n")
    _w(root, "packages/core/src/util.ts", "export const util = () => core;\n")
    _w(root, "packages/web/src/app.ts", "import { core } from '@demo/core';\nexport default core;\n")
    _w(root, "packages/web/src/app.spec.ts", "it('works', () => expect(1).toBe(1));\n")
    _w(root, "tsconfig.json", '{\n  "compilerOptions": {"strict": true}\n}\n')
    # 噪声：装出来的依赖 / 产物 / 纯类型声明
    _w(root, "node_modules/lodash/package.json", '{"name": "lodash"}\n')
    _w(root, "node_modules/lodash/index.js", "module.exports = {};\n")
    _w(root, "packages/core/dist/index.js", "exports.core = 1;\n")
    _w(root, "packages/core/src/types.d.ts", "export declare const core: number;\n")
    return WorkspaceFixture(
        name="npm_workspaces", root=root, stack="npm", lang="node",
        aggregate_manifest="package.json",
        registered=("packages/core",),
        unregistered=("packages/web",),
        module_manifests=("packages/core/package.json", "packages/web/package.json"),
        sources=("packages/core/src/index.ts", "packages/core/src/util.ts",
                 "packages/web/src/app.ts", "packages/web/src/app.spec.ts"),
        noise=("node_modules/lodash/index.js", "packages/core/dist/index.js",
               "packages/core/src/types.d.ts"),
        topology="workspace",
    )


# ══════════════════════════════════════════════════════════════════
# go.work —— R-1 主犯：判死却无人收敛
# ══════════════════════════════════════════════════════════════════

def build_go_work(root: Path) -> WorkspaceFixture:
    """go.work 多模块。**模块必须是根直接子目录**——`_reconcile_go_work` 走
    `_safe_subdirs(root)` 只扫一层，塞进 `svc/auth` 就走不进对账分支（假绿的经典形状）。

    `use (...)` 用**块形式**（`go work use` 的默认产物），正是 C4 那条"块内只捕获首成员 →
    重复 use → go fatal"治本的现场；夹具沿用块形式才测得到它。
    """
    _w(root, "go.work", "go 1.22\n\nuse (\n\t./auth\n)\n")
    _w(root, "auth/go.mod", "module example.com/app/auth\n\ngo 1.22\n")
    _w(root, "gateway/go.mod", "module example.com/app/gateway\n\ngo 1.22\n")
    _w(root, "auth/token.go", "package auth\n\nfunc Token() string { return \"t\" }\n")
    _w(root, "auth/token_test.go", "package auth\n\nimport \"testing\"\n\nfunc TestToken(t *testing.T) {}\n")
    _w(root, "gateway/main.go", "package main\n\nfunc main() {}\n")
    # 噪声：vendored 依赖（从不由人手写）
    _w(root, "auth/vendor/github.com/x/y/y.go", "package y\n")
    return WorkspaceFixture(
        name="go_work", root=root, stack="go", lang="go",
        aggregate_manifest="go.work",
        registered=("auth",),
        unregistered=("gateway",),
        module_manifests=("auth/go.mod", "gateway/go.mod"),
        sources=("auth/token.go", "auth/token_test.go", "gateway/main.go"),
        noise=("auth/vendor/github.com/x/y/y.go",),
        topology="workspace",
    )


def build_go_single_root(root: Path) -> WorkspaceFixture:
    """单根 Go 仓：**一个** `go.mod` 管整仓，`cmd/` `internal/` 只是包目录，不是模块。

    ★L5 血泪的夹具化★ "把每个顶层目录当独立构建单元"曾让 Go 单根/Rust 单 crate 的补丁
    被静默丢弃 → abandoned → PARTIAL。这型的正确期望是：**没有** per-module 清单、
    **没有** 聚合登记、`_reconcile_go_work` 一个字都不该写（连 go.work 都不许擅自创建——
    单模块库无须工作区，擅自建会改变构建语义）。
    """
    _w(root, "go.mod", "module example.com/single\n\ngo 1.22\n")
    _w(root, "main.go", "package main\n\nfunc main() {}\n")
    _w(root, "internal/store/store.go", "package store\n\ntype S struct{}\n")
    _w(root, "cmd/serve/serve.go", "package main\n\nfunc main() {}\n")
    _w(root, "vendor/github.com/x/y/y.go", "package y\n")
    return WorkspaceFixture(
        name="go_single_root", root=root, stack="go", lang="go",
        aggregate_manifest=None,
        registered=(), unregistered=(),
        module_manifests=(),
        sources=("main.go", "internal/store/store.go", "cmd/serve/serve.go"),
        noise=("vendor/github.com/x/y/y.go",),
        topology="single-root",
    )


# ══════════════════════════════════════════════════════════════════
# Cargo workspace
# ══════════════════════════════════════════════════════════════════

def build_cargo_workspace(root: Path) -> WorkspaceFixture:
    """虚拟 manifest 根（只有 `[workspace]`，无 `[package]`）+ `crates/<x>` 成员。

    `_reconcile_cargo` 扫**根 + 一层子目录**，认"含 `[package]` 的 Cargo.toml"，且
    members 数组含 `#` 行内注释时**整体跳过**（重排会丢注释）——夹具刻意不放注释，
    否则测试走不进对账分支。
    """
    _w(root, "Cargo.toml",
       '[workspace]\nmembers = [\n    "crates/core",\n]\nresolver = "2"\n')
    _w(root, "crates/core/Cargo.toml",
       '[package]\nname = "core"\nversion = "0.1.0"\nedition = "2021"\n')
    _w(root, "crates/api/Cargo.toml",
       '[package]\nname = "api"\nversion = "0.1.0"\nedition = "2021"\n')
    _w(root, "crates/core/src/lib.rs", "pub fn core() -> u8 { 1 }\n")
    _w(root, "crates/api/src/main.rs", "fn main() { println!(\"api\"); }\n")
    _w(root, "crates/api/src/routes.rs", "pub fn routes() {}\n")
    # 噪声：构建产物
    _w(root, "target/debug/build.rs", "fn main() {}\n")
    return WorkspaceFixture(
        name="cargo_workspace", root=root, stack="cargo", lang="rust",
        aggregate_manifest="Cargo.toml",
        registered=("crates/core",),
        unregistered=("crates/api",),
        module_manifests=("crates/core/Cargo.toml", "crates/api/Cargo.toml"),
        sources=("crates/core/src/lib.rs", "crates/api/src/main.rs",
                 "crates/api/src/routes.rs"),
        noise=("target/debug/build.rs",),
        topology="workspace",
    )


# ══════════════════════════════════════════════════════════════════
# Gradle KTS —— 别名档（F-1 的现场）
# ══════════════════════════════════════════════════════════════════

def build_gradle_kts(root: Path) -> WorkspaceFixture:
    """`settings.gradle.kts` + `build.gradle.kts` —— 全走 **`.kts` 别名**。

    ★这一型专治 F-1★ spec 声明 `.kts` 与 canonical"同档消费"，但两个消费者当初都只读
    单数主字段 → KTS 工程根清单脚手架 `bumped=0`、规则4 登记整体沉默（R-2 在 gradle-kts
    上原样活着）。别名整列落空是"接线覆盖 ≠ 机制存在"的复发形态，只有 KTS 夹具照得出。

    模块必须是**根直接子目录**（`_reconcile_gradle` 注释写明"仅处理顶层"），且脚本里
    绝不出现 `file(` / `rootDir` / `subprojects {`——那些会命中 `_GRADLE_DYNAMIC`
    启发式 → 整个对账被跳过（又一处"夹具让测试走不进被测分支"的坑）。
    """
    _w(root, "settings.gradle.kts",
       'rootProject.name = "demo"\n\ninclude(":app")\n')
    _w(root, "build.gradle.kts", 'plugins {\n    kotlin("jvm") version "1.9.22"\n}\n')
    _w(root, "app/build.gradle.kts", 'plugins {\n    kotlin("jvm")\n}\n')
    _w(root, "lib/build.gradle.kts", 'plugins {\n    kotlin("jvm")\n}\n')
    _w(root, "app/src/main/kotlin/App.kt", "fun main() {}\n")
    _w(root, "lib/src/main/kotlin/Lib.kt", "class Lib\n")
    _w(root, "lib/src/main/java/Legacy.java", "class Legacy {}\n")
    # 噪声：构建产物
    _w(root, "app/build/classes/App.class", "\n")
    _w(root, "build/reports/index.html", "<html></html>\n")
    return WorkspaceFixture(
        name="gradle_kts", root=root, stack="gradle", lang="java",
        aggregate_manifest="settings.gradle.kts",
        registered=("app",),
        unregistered=("lib",),
        module_manifests=("app/build.gradle.kts", "lib/build.gradle.kts"),
        sources=("app/src/main/kotlin/App.kt", "lib/src/main/kotlin/Lib.kt",
                 "lib/src/main/java/Legacy.java"),
        noise=("app/build/classes/App.class",),
        topology="workspace",
    )


# ══════════════════════════════════════════════════════════════════
# Maven reactor —— 对照臂（防"只为异栈放宽"）
# ══════════════════════════════════════════════════════════════════

def build_maven_reactor(root: Path) -> WorkspaceFixture:
    """标准 reactor。**存在的意义是对照**：矩阵里每条断言在 Maven 上也必须成立。

    只跑异栈的矩阵有个隐患——改判据时把 Maven 一起放宽了也照样绿。子模块必须声明
    `<parent>`（`_reconcile_maven` 只注册本工程子模块，独立工程目录不碰）。
    """
    _w(root, "pom.xml",
       '<?xml version="1.0" encoding="UTF-8"?>\n<project>\n'
       "  <groupId>com.demo</groupId>\n  <artifactId>demo</artifactId>\n"
       "  <version>1.0.0</version>\n  <packaging>pom</packaging>\n"
       "  <modules>\n        <module>svc-core</module>\n    </modules>\n</project>\n")
    for m in ("svc-core", "svc-web"):
        _w(root, f"{m}/pom.xml",
           "<project>\n  <parent><groupId>com.demo</groupId>"
           "<artifactId>demo</artifactId><version>1.0.0</version></parent>\n"
           f"  <artifactId>{m}</artifactId>\n</project>\n")
    _w(root, "svc-core/src/main/java/com/demo/Core.java", "package com.demo;\nclass Core {}\n")
    _w(root, "svc-web/src/main/java/com/demo/Web.java", "package com.demo;\nclass Web {}\n")
    _w(root, "svc-web/src/main/kotlin/com/demo/Ext.kt", "package com.demo\nclass Ext\n")
    _w(root, "svc-core/target/classes/Core.class", "\n")
    return WorkspaceFixture(
        name="maven_reactor", root=root, stack="maven", lang="java",
        aggregate_manifest="pom.xml",
        registered=("svc-core",),
        unregistered=("svc-web",),
        module_manifests=("svc-core/pom.xml", "svc-web/pom.xml"),
        sources=("svc-core/src/main/java/com/demo/Core.java",
                 "svc-web/src/main/java/com/demo/Web.java",
                 "svc-web/src/main/kotlin/com/demo/Ext.kt"),
        noise=("svc-core/target/classes/Core.class",),
        topology="reactor",
    )


# ══════════════════════════════════════════════════════════════════
# 注册表（单一事实源：加一型 = 加一条，parametrize 自动跟上）
# ══════════════════════════════════════════════════════════════════

WORKSPACE_BUILDERS = {
    "npm_workspaces": build_npm_workspaces,
    "go_work": build_go_work,
    "go_single_root": build_go_single_root,
    "cargo_workspace": build_cargo_workspace,
    "gradle_kts": build_gradle_kts,
    "maven_reactor": build_maven_reactor,
}

NON_MAVEN_WORKSPACES = tuple(k for k in WORKSPACE_BUILDERS if k != "maven_reactor")
"""非 Maven 型（B-0 的红灯面）。Maven 单独作对照臂，不混进"异栈"参数集。"""

AGGREGATE_WORKSPACES = tuple(
    k for k in WORKSPACE_BUILDERS if k != "go_single_root"
)
"""有根聚合清单的型（single-root 无聚合，聚合档断言对它无意义）。"""


def build_workspace(name: str, root: Path) -> WorkspaceFixture:
    """按名造树。未知名 **大声失败**（绝不静默回退某一型——那会让 parametrize 假绿）。"""
    try:
        builder = WORKSPACE_BUILDERS[name]
    except KeyError:
        raise KeyError(
            f"未知 workspace 夹具 {name!r}；已注册：{sorted(WORKSPACE_BUILDERS)}") from None
    root.mkdir(parents=True, exist_ok=True)
    return builder(root)
