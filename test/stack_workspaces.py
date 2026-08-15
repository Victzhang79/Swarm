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
| `npm_workspaces` | workspace | `package.json` `workspaces` | X-H3 现场：显式列表形态不自愈（`_reconcile_npm` 落地前新子包 `npm ci` 装不到） |
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
    """**不**参与编译的文件（vendored / 产物 / 纯声明）——必须全判 False。

    ★必须含**该栈真源码后缀**的产物★（复核 MEDIUM-1）：只放 `.class` 之类本就不在
    `source_exts` 的文件，判 False 与 `source_exclude_dirs` **无关** → 那一格的排除目录
    删掉也不会红（maven/gradle 两型当初就是这样恒真的）。真实危害面是
    `target/generated-sources/**/*.java`、`build/generated/**/*.kt` 这类
    annotation processor / kapt 产物：被当人写源码会让 R67-T9 难度虚高，且
    `classpath_fqn_key` 给生成类算出 FQN → 跨模块同名判据误伤。"""

    decoy_manifests: tuple[str, ...] = field(default_factory=tuple)
    """埋在**被排除目录**里、形状足以骗过 `_reconcile_*` 成员判据的清单。

    ★为什么必须有（复核 HIGH-1）★ `_reconcile_*` 只"补漏"不"防多"，而矩阵原先只断
    `unregistered ⊆ added`（超集），于是**过度登记全程隐形**：实测把 `_SKIP_DIRS` 清空，
    `target/staging/Cargo.toml` 当场被登记成 workspace 成员（＝R46-2 幽灵成员 / L8 泄漏
    那一族），而测试照旧全绿。这些诱饵 + `added` **断相等**才锁得住 `_SKIP_DIRS`
    ——那张表此前**全仓无人锁**。"""

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

    ★npm 的 `unregistered` 曾是**真缺口**★（X-H3：无 `_reconcile_npm` 时没人补 web，
    `npm ci` 装不到它）。`_reconcile_npm` 落地（B-5）后本夹具转成 reconcile 的
    正向锁：显式列表形态必须补 web（glob 形态自愈不在此列）。夹具如实造出
    显式列表形状，别替实现圆场。
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
        # 诱饵：装出来的包本身就是个合法 package.json，`_SKIP_DIRS` 是唯一拦它的东西
        decoy_manifests=("node_modules/lodash/package.json",),
        topology="workspace",
    )


# ══════════════════════════════════════════════════════════════════
# python —— 缺口最多的栈，此前**唯一无夹具**的已收录栈
# ══════════════════════════════════════════════════════════════════

def build_python_workspace(root: Path) -> WorkspaceFixture:
    """`pyproject.toml` + `src/` 布局。

    ★为什么必须补（复核 HIGH-2/MEDIUM-3）★ python 是 `STACK_SPEC` 六栈里缺口最多的一个
    （`aggregate_manifest=None`、两档 reconcile/driver 皆无、R-3 曾对 `.py` 全程失效），
    而它此前**唯一没有夹具**，两道准入闸都只管"表里有没有这一行"、不管"夹具矩阵里有没有
    这一型" → B-0 的使命是红灯先行，少这一型＝**少点亮一条本该当场亮的红灯**
    （实测：python 工程 `_derive_full_build_command` 返 `''` ＝ 27 号文 V-C1/X-H1 的 python 行）。

    ★源码必须落在**可解析的布局段**下★（`src/`）——`_module_physical_dirs` 对无标准布局段的
    路径恒返 `{}`，那会让"没注入脚手架"变成**与栈路由无关地恒真**（reviewer HIGH-2 实测：
    把 `pyproject.toml` 从路由表摘掉、纯 py 仓落进 Maven 兜底，而矩阵那格照旧绿）。
    """
    _w(root, "pyproject.toml",
       '[project]\nname = "demo"\nversion = "0.1.0"\n'
       'requires-python = ">=3.11"\n')
    _w(root, "src/mod_a/__init__.py", '"""mod_a."""\n')
    _w(root, "src/mod_a/service.py", "def serve() -> int:\n    return 1\n")
    _w(root, "src/mod_a/repo.py", "def fetch() -> None:\n    return None\n")
    _w(root, "src/mod_b/__init__.py", '"""mod_b."""\n')
    # 噪声：构建产物 / 虚拟环境（`.py` 后缀，真源码后缀 → 排除目录是唯一拦它的东西）
    _w(root, "build/lib/mod_a/service.py", "def serve() -> int:\n    return 1\n")
    _w(root, "dist/mod_a/service.py", "def serve() -> int:\n    return 1\n")
    _w(root, ".venv/lib/site-packages/dep/mod.py", "X = 1\n")
    return WorkspaceFixture(
        name="python_workspace", root=root, stack="python", lang="python",
        # ★刻意 None★ poetry/uv/hatch 的 workspace 机制互不兼容，STACK_SPEC 显式未收录
        aggregate_manifest=None,
        registered=(), unregistered=(),
        module_manifests=(),
        sources=("src/mod_a/__init__.py", "src/mod_a/service.py",
                 "src/mod_a/repo.py", "src/mod_b/__init__.py"),
        noise=("build/lib/mod_a/service.py", "dist/mod_a/service.py",
               ".venv/lib/site-packages/dep/mod.py"),
        topology="single-root",
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
    # ★块里必须**两个**成员（复核 MEDIUM-3）★ C4 的病灶是"块内只捕获**首**成员"——
    # 单成员块下病灶版与治本版结果**完全一样**，夹具"沿用块形式才测得到它"这句话就落空了。
    _w(root, "go.work", "go 1.22\n\nuse (\n\t./auth\n\t./shared\n)\n")
    _w(root, "auth/go.mod", "module example.com/app/auth\n\ngo 1.22\n")
    _w(root, "shared/go.mod", "module example.com/app/shared\n\ngo 1.22\n")
    _w(root, "gateway/go.mod", "module example.com/app/gateway\n\ngo 1.22\n")
    _w(root, "auth/token.go", "package auth\n\nfunc Token() string { return \"t\" }\n")
    _w(root, "auth/token_test.go", "package auth\n\nimport \"testing\"\n\nfunc TestToken(t *testing.T) {}\n")
    _w(root, "shared/util.go", "package shared\n\nfunc Util() {}\n")
    _w(root, "gateway/main.go", "package main\n\nfunc main() {}\n")
    # 噪声：vendored 依赖（从不由人手写）
    _w(root, "auth/vendor/github.com/x/y/y.go", "package y\n")
    # 诱饵：根级 vendor 里放个合法 go.mod —— `_SKIP_DIRS` 是唯一拦它进 `use` 的东西
    _w(root, "vendor/github.com/x/y/go.mod", "module github.com/x/y\n\ngo 1.22\n")
    return WorkspaceFixture(
        name="go_work", root=root, stack="go", lang="go",
        aggregate_manifest="go.work",
        registered=("auth", "shared"),
        unregistered=("gateway",),
        module_manifests=("auth/go.mod", "shared/go.mod", "gateway/go.mod"),
        sources=("auth/token.go", "auth/token_test.go", "shared/util.go",
                 "gateway/main.go"),
        noise=("auth/vendor/github.com/x/y/y.go",),
        decoy_manifests=("vendor/github.com/x/y/go.mod",),
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
    # 诱饵：`target/staging` 里放个带 [package] 的 Cargo.toml。**实测**（复核 HIGH-1）：
    # 把 `_SKIP_DIRS` 清空 → added 变成 ['crates/api', 'target/staging']，产物目录被登记
    # 成 workspace 成员，而原先只断超集的测试照旧全绿。
    _w(root, "target/staging/Cargo.toml",
       '[package]\nname = "staging"\nversion = "0.1.0"\nedition = "2021"\n')
    return WorkspaceFixture(
        name="cargo_workspace", root=root, stack="cargo", lang="rust",
        aggregate_manifest="Cargo.toml",
        registered=("crates/core",),
        unregistered=("crates/api",),
        module_manifests=("crates/core/Cargo.toml", "crates/api/Cargo.toml"),
        sources=("crates/core/src/lib.rs", "crates/api/src/main.rs",
                 "crates/api/src/routes.rs"),
        noise=("target/debug/build.rs",),
        decoy_manifests=("target/staging/Cargo.toml",),
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
    绝不出现 `file(`（include 邻近除外——批23 C-6b#5 后仅 `include file(` 邻近形态命中）
    / `rootDir.<迭代方法>`（批26 后裸 `rootDir` 不再命中，仅迭代基座邻近式命中）
    / `subprojects {`——那些会命中 `_gradle_dynamic_hit`
    启发式 → 整个对账被跳过（又一处"夹具让测试走不进被测分支"的坑）。
    """
    _w(root, "settings.gradle.kts",
       'rootProject.name = "demo"\n\ninclude(":app")\n')
    _w(root, "build.gradle.kts", 'plugins {\n    kotlin("jvm") version "1.9.22"\n}\n')
    _w(root, "app/build.gradle.kts", 'plugins {\n    kotlin("jvm")\n}\n')
    _w(root, "lib/build.gradle.kts", 'plugins {\n    kotlin("jvm")\n}\n')
    # ★源码必须在**包目录**下★ `classpath_fqn_key`（JVM 系的难度判据）要求源码根之后还有
    # 包路径：`com/demo/App.kt` 可解析，裸 `App.kt` 返 None。裸包文件在 Kotlin 里合法且常见，
    # 但 JVM 侧那条"额外要求可定位物理模块根"是**刻意的既有更严口径**（R67-T9 原注写明保持
    # 不动），不是本批要翻的账——夹具按主流形态给包目录，让矩阵量到 R-3 真正那条命题。
    _w(root, "app/src/main/kotlin/com/demo/App.kt", "package com.demo\n\nfun main() {}\n")
    _w(root, "app/src/main/kotlin/com/demo/Cfg.kt", "package com.demo\n\nclass Cfg\n")
    _w(root, "lib/src/main/kotlin/com/demo/Lib.kt", "package com.demo\n\nclass Lib\n")
    _w(root, "lib/src/main/java/com/demo/Legacy.java",
       "package com.demo;\nclass Legacy {}\n")
    # 噪声：构建产物。★必须含真源码后缀★（复核 MEDIUM-1）——`.class` 本就不在 source_exts，
    # 判 False 与排除目录无关；kapt 产物的 `.kt` 才是这一栈真实的危害面。
    _w(root, "app/build/classes/App.class", "\n")
    _w(root, "build/reports/index.html", "<html></html>\n")
    _w(root, "app/build/generated/source/kapt/main/Gen.kt", "class Gen\n")
    _w(root, "lib/build/generated/source/kapt/main/GenLib.java", "class GenLib {}\n")
    # 诱饵：产物目录里放个 build.gradle.kts（`_safe_subdirs` 是唯一拦它被 include 的东西）
    _w(root, "build/staging/build.gradle.kts", 'plugins {\n    kotlin("jvm")\n}\n')
    return WorkspaceFixture(
        name="gradle_kts", root=root, stack="gradle", lang="java",
        aggregate_manifest="settings.gradle.kts",
        registered=("app",),
        unregistered=("lib",),
        module_manifests=("app/build.gradle.kts", "lib/build.gradle.kts"),
        sources=("app/src/main/kotlin/com/demo/App.kt",
                 "app/src/main/kotlin/com/demo/Cfg.kt",
                 "lib/src/main/kotlin/com/demo/Lib.kt",
                 "lib/src/main/java/com/demo/Legacy.java"),
        noise=("app/build/classes/App.class",
               "app/build/generated/source/kapt/main/Gen.kt",
               "lib/build/generated/source/kapt/main/GenLib.java"),
        decoy_manifests=("build/staging/build.gradle.kts",),
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
    # ★真源码后缀的产物★（复核 MEDIUM-1）：annotation processor 生成的 `.java` 是这一栈
    # 真实存在的危害面——被当人写源码则 R67-T9 难度虚高，且 classpath_fqn_key 会给生成类
    # 算出 FQN → 跨模块同名判据误伤。只放 `.class` 时这一格恒真（删掉 target 排除也不红）。
    _w(root, "svc-core/target/generated-sources/annotations/com/demo/Gen.java",
       "package com.demo;\nclass Gen {}\n")
    # 诱饵：产物目录里放个带 <parent> 的 pom（`_safe_subdirs` 是唯一拦它进 <modules> 的东西）
    _w(root, "target/staging/pom.xml",
       "<project>\n  <parent><groupId>com.demo</groupId>"
       "<artifactId>demo</artifactId><version>1.0.0</version></parent>\n"
       "  <artifactId>staging</artifactId>\n</project>\n")
    return WorkspaceFixture(
        name="maven_reactor", root=root, stack="maven", lang="java",
        aggregate_manifest="pom.xml",
        registered=("svc-core",),
        unregistered=("svc-web",),
        module_manifests=("svc-core/pom.xml", "svc-web/pom.xml"),
        sources=("svc-core/src/main/java/com/demo/Core.java",
                 "svc-web/src/main/java/com/demo/Web.java",
                 "svc-web/src/main/kotlin/com/demo/Ext.kt"),
        noise=("svc-core/target/classes/Core.class",
               "svc-core/target/generated-sources/annotations/com/demo/Gen.java"),
        decoy_manifests=("target/staging/pom.xml",),
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
    "python_workspace": build_python_workspace,
    "maven_reactor": build_maven_reactor,
}

# 注：**刻意不提供** `NON_MAVEN_WORKSPACES`。它此前零消费者（全仓唯一出现处是一句注释），
# 而同一批 commit 还专门以"新账没有消费者＝没造"为由删掉了用它的参数化夹具——纪律没执行
# 到底（复核 LOW-1/L-4）。要遍历异栈的消费者请就地写 parametrize，覆盖范围一目了然。

NO_AGGREGATE_WORKSPACES = ("go_single_root", "python_workspace")
"""**无**根聚合清单的型：go 单根仓没用 go.work；python 的聚合机制 STACK_SPEC 显式未收录。"""

AGGREGATE_WORKSPACES = tuple(
    k for k in WORKSPACE_BUILDERS if k not in NO_AGGREGATE_WORKSPACES
)
"""有根聚合清单的型（聚合档断言只对它们有意义）。

★这份手抄排除名单由 `test_aggregate_param_set_matches_fixture_facts` 与夹具自报的
`aggregate_manifest is None` 对账★——两者分叉就会静默漏测一整型，而参数计数看起来毫无异常。"""


def build_workspace(name: str, root: Path) -> WorkspaceFixture:
    """按名造树。未知名 **大声失败**（绝不静默回退某一型——那会让 parametrize 假绿）。"""
    try:
        builder = WORKSPACE_BUILDERS[name]
    except KeyError:
        raise KeyError(
            f"未知 workspace 夹具 {name!r}；已注册：{sorted(WORKSPACE_BUILDERS)}") from None
    root.mkdir(parents=True, exist_ok=True)
    return builder(root)
