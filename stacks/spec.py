"""L1 事实表：一张 `STACK_SPEC` 取代散落全仓的清单名单（27 号文 §6.2）。

## 为什么要这张表（B-0 实测的 R-1，不是设计洁癖）

规划期有两处名单，一处**判死**、一处**收敛**，而它们不是同一张表：

| 清单 | 硬失败闸认不认 | 收敛器收不收 | 后果 |
|---|---|---|---|
| `pom.xml` | 认 | 收 | 正常 |
| `go.work` / `settings.gradle` / `Cargo.toml` | 认 | **不收** | **判死却无人能救** |
| `package.json` | **不认** | 不收 | 双写者放行 |

go/gradle/cargo 侧实测 `before.valid=False` →（确定性 pass 全跑完）→ `after.valid=False`，
issue 逐字不变 → 同签名连续两轮 → **熔断 fail-fast**。也就是说：**任何 Go/Gradle/Cargo
多模块工程，只要 LLM 让两个子任务都注册模块，就 100% 死在规划期**，而日志里只有
"同签名不收敛"，看不出根因是"没人会收敛 go.work"。npm 侧则相反——漏判 → 两个子任务
先后重写根 `package.json` 的 `workspaces` 数组，而聚合结构重写**非加性** → 后写者覆盖
前写者的注册 → 丢 workspace。两份名单一个多一个少，互为对方的反证。

## ★字段按【消费后果】分档，不是一张扁平名单★

本仓吃过的亏：共享表可以共享，**后果不同就必须分档**（密钥模式表的 warn-only 档被接到
"拒绝即删存量"的闸上当场冤杀；知识库"隐藏目录=噪声"语义搬去剔 tarball 把 `.mvn/wrapper`
剔没了）。所以这里刻意**不提供**一个叫 `manifests` 的大杂烩集合，而是每种问题一个字段：

- `aggregate_manifest`：聚合**登记**落点（`<modules>` / `include` / `use` / `members` /
  `workspaces`）。判错的后果 = 双写者非加性覆盖，或判死无人收敛。
- `module_manifest`：每模块清单。判错的后果 = 脚手架落点错。
- `root_manifests`：判"这是不是该栈的工程"。判错的后果 = 栈识别错。
- `source_exts` + `shares_classpath_namespace`：判"这是不是参与编译的源码"。
  判错的后果 = 难度路由错 / 跨模块同名判据误伤。

三者**故意可以重叠**（npm 的 `package.json` 三个字段都是它），但**语义不可互换**——
调用方必须按自己的后果选字段，不许拿 `root_manifests` 去做聚合闸。

## 诚实边界：不收录 ≠ 不存在

`aggregate_manifest=None` 表示"该栈的聚合登记机制本表**未收录**"，不表示"该栈没有
聚合"。python 生态的 workspace 是碎的（poetry / uv / hatch 各写各的），此刻收录任何一
种都是猜——故显式 None，并由 `unregistered_aggregate_stacks()` 让这份缺席**机读可辨**，
而不是让它伪装成"已覆盖"。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StackSpec:
    """一个构建栈的确定性事实。**只放事实，不放策略**——策略在 L2 driver。"""

    key: str
    """栈键：maven|gradle|npm|go|cargo|python。与既有 `_stack_driver_keys` 口径一致。"""

    lang: str
    """语言一等字段：java|node|go|rust|python。停止用字符串往返猜语言。"""

    root_manifests: tuple[str, ...]
    """判"仓库根是不是该栈工程"的清单名（**规范大小写**——Linux 上 `os.path.exists`
    大小写敏感，写小写会漏检 `Cargo.toml`）。"""

    module_manifest: str
    """每模块清单名（脚手架落点 `<module>/<module_manifest>`）。"""

    aggregate_manifest: str | None
    """**聚合登记**落点（根级）。None = 本表未收录该栈的聚合机制（见模块 docstring 诚实边界）。

    ★这是 R-1 的单一事实源★：判死的闸与收敛的 pass 必须都读它，不许各存一份。"""

    aggregate_field: str
    """聚合登记在清单里的**字段名**，用于生成人读的验收条目（`<modules>` / `workspaces` …）。
    aggregate_manifest 为 None 时无意义。"""

    source_exts: tuple[str, ...]
    """参与编译的源码后缀（资源/清单/文档不算）。"""

    shares_classpath_namespace: bool = False
    """该栈的源码是否共享**类路径命名空间**（JVM 系 True）。

    这是 `classpath_fqn_key` 门控的单一事实源：JVM 系必须按包限定 FQN 判同名冲突，
    非 JVM 系没有这个概念（Go 按目录、Rust 按 crate、npm 按路径），拿 JVM 判据套过去
    会恒返 None——R67-T9 的"多源码文件难度提升"就是这样对所有非 JVM 栈从未生效过。"""

    aggregate_extra_manifests: tuple[str, ...] = field(default_factory=tuple)
    """同一栈的聚合清单**别名**（Gradle 的 `.kts` 变体）。与 aggregate_manifest 同档消费。

    ★消费方必须走 `aggregate_manifests_of_stack()` 取【全集】★：只读 `aggregate_manifest`
    单数字段 = 别名整列落空（双复核实测：`settings.gradle.kts` 工程 bump=0、规则4 登记沉默，
    R-2 在 gradle-kts 上原样活着）。这是"接线覆盖 ≠ 机制存在"在本表内的复发形态。"""

    module_extra_manifests: tuple[str, ...] = field(default_factory=tuple)
    """同一栈的模块清单**别名**（`build.gradle.kts`）。与 module_manifest 同档消费。
    同上：消费方走 `module_manifests_of_stack()` 取全集，别只读单数字段。"""

    has_aggregate_reconcile: bool = False
    """`worker/workspace_manifest.py` 是否有该栈**聚合清单**的 `_reconcile_*`
    （据磁盘 ground-truth 补齐根级注册）。

    ★这是"根聚合 demote 安不安全"的事实依据★：demote 收回非 owner 写者的写权，有 reconcile
    时丢的**注册**会在 L1/L2/交付三处补回（+规则4 owner 登记＝双保险）；没有时只剩规则4
    这一道网。实测（2026-07-30）：maven `_reconcile_maven` / gradle `_reconcile_gradle`（认
    `.kts`）/ cargo `_reconcile_cargo` / go `_reconcile_go_work` 有，**npm 无 `_reconcile_npm`**。

    ★不可外推到模块清单★ 这些函数只补聚合登记（modules/include/members/use），
    模块清单档看 `has_module_scaffold_driver`（复核 M-3：字段事实=聚合级，被消费成"任何
    清单 demote 都有兜底"→ 当时 go/gradle/cargo 的模块清单丢贡献连 WARNING 都没有；
    后由 #31-P2c/2e/2f driver 全部补齐——P-H4 三臂落地，gradle 是最后一臂）。"""

    aggregate_reconcile_members_only: bool = False
    """该栈的 `_reconcile_*` 是否**只补成员注册**（不兜底整文件其它字段的编辑）。

    ★X-H3 R2 hunter CRITICAL★ npm 的 `_reconcile_npm` 只补 `workspaces` 成员列表；
    demote 收回的是【整个文件的写权】——scripts/dependencies/devDependencies 的真实
    编辑 reconcile 补不回。`has_aggregate_reconcile=True` 是事实（函数存在），但拿它
    当"demote 整文件安全"用 = 「复用单一事实源 ≠ 复用其消费契约」复发：WARNING 被
    静默关掉，scripts/dependencies 编辑蒸发零信号。本字段=True 时
    `demote_safety_net` 聚合档仍判【不安全】（照常 WARNING，文案说明注册有兜底、
    其它字段没有）。maven/gradle/cargo/go 的聚合清单本身就是纯结构文件
    （modules/include/members/use 之外几乎无独立编辑面）→ False。
    """

    has_module_scaffold_driver: bool = False
    """该栈是否有**确定性脚手架 driver 一次建全模块清单**（= contract_utils
    `_MODULE_SCAFFOLD_DRIVER_STACKS`——maven 聚合 driver ∪ `_P2_SCAFFOLD_DRIVERS`，
    由 `test_scaffold_driver_facts_match_reality` 对账防漂移）。

    ★这是"模块清单 demote 安不安全"的事实依据★：owner 按契约一次建全模块清单时，非 owner
    写者本就没有该文件的合法贡献，demote 无损（#11a doctrine）。没有 driver 的栈里 demote
    掉的是**真实编辑**（该子任务想加的依赖/插件），且无任何 reconcile 覆盖模块清单
    → 必须 WARNING + record_degrade（纪律 3）。

    ★已知边界（hunter R2 M-3，登记不改）★ 这是**栈级**事实，而 driver 只覆盖【契约
    dependencies 里的模块】——契约外模块的清单 demote 仍无网（G1 coherence 大概率先把
    无契约模块的 plan 打回，实际可达性低）。拿栈级事实当"任何模块清单都有网"与 M-3
    原病同形，只是可达性低一档；真收窄需要 demote 留痕点能反查模块契约归属，待 P-M。"""

    source_exclude_dirs: tuple[str, ...] = field(default_factory=tuple)
    """判"参与编译源码"时要排除的目录段（vendored / 产物目录，从不由人手写）。"""

    whole_project_build_cmd: str = ""
    """**整工程**全量编译命令（L2/集成复核用）。空串＝本表未收录该栈的整工程命令。

    ★为什么放在这里（M-1/M-6，用户拍板"L2 复用 L1 的实现"）★
    `integration_review._detect_build_cmd_generic` 与 `l1_pipeline._derive_full_build_command`
    是**同职责 sibling**（清单→构建命令），此前各写一份 if 链 ⇒ 必然漂移。本会话已被同类分叉
    咬过三次（`_manifest_present` 本地/沙箱、`_build_cmd_applicable` 漏调用点、两探针排除表）。

    ★但两层的 scope 语义**不同**，不能直接互调★：
      · L1 是**子任务级**——命令要锚到改动文件所在清单目录，python 甚至只编译改动的那几个文件
        （用户拍板：linter 仓刻意 ship 坏语法夹具，整树必永久冤枉）；
      · L2 是**整工程级**——merge 后必须编译**全部**代码，只编改动文件等于没验。
    所以共享的是"**哪个栈用什么命令**"这份事实，而不是 scope 策略：L2 读本字段，L1 在自己的
    scope 逻辑里读同一张表挑栈。一份事实、两种投影。"""

    test_cmd: str = ""
    """**工程级**测试命令（L1.3 兜底用）。空串＝本表未收录该栈的工程级测试命令。

    与 `whole_project_build_cmd` 同源：`_guess_test_cmd` 此前把各栈测试字面量硬编码在函数
    内部，新增栈/改命令时 L1.3 与 L2 口径漂移。本字段提供"有确定性证据时出什么命令"的
    单一事实源；scoped 测试（按改动文件找同名测试文件）仍由调用方按需包裹，但工程级兜底
    必须从这里取。
    ★JVM 系刻意留空★：brain/nodes/shared.py 给 java 的 test_command 故意留空
    （RuoYi 等项目常无测试依赖，强跑必失败）。本表不私自放行。"""

    test_priority: int = 100
    """多栈仓里 `_guess_test_cmd` 的栈胜出优先序（小者先）。★W-24（#29-5 挂账，用户
    拍板维持现序）★：原实现把栈集合与顺序写死在 `_guess_test_cmd` 的元组
    `("python","go","cargo","npm")`——栈集合复制了 TEST_DRIVERS 键集（新栈加 test_cmd
    也进不了循环=静默零覆盖），且「JVM 刻意不猜」被 test_cmd="" 与该元组**两处编码**
    （任一单独突变仍绿=不可证伪，xh_exec 锁因此一度撤销；复活时又逮到第三处编码=
    `_ext_for_lang` 手写小表，已一并改为从本表 source_exts 派生）。收进本字段后：栈集合由
    TEST_DRIVERS 派生（本表即单一事实源），顺序由本字段显式声明——数值维持原元组序
    （python=10 < go=20 < cargo=30 < npm=40），新栈加 test_cmd 时必须同时选档，不存在
    "悄悄插进默认位"的路径。"""

    runtime_start_cmd: str = ""
    """运行时**默认启动命令**（verify_runtime S1-2）。空串＝本栈无统一默认命令，
    由 `brain/smoke_derive.py` 按磁盘证据动态推导。

    ★BRAIN-002★ 此前启动命令与 prepare 命令散落在 smoke_derive 的硬编码数据表与推导函数
    中，改命令/新增栈时与 L1/L2 构建命令漂移。本字段把"启动怎么跑"的事实收进 STACK_SPEC，
    与 `whole_project_build_cmd` / `test_cmd` 同源；需要证据的部分（如 JVM jar 路径前缀、
    node scripts 名、python 入口文件）仍由推导器按磁盘决定，但命令字面量从这里取。"""

    runtime_prepare_cmd: str = ""
    """start_cmd 消费**构建产物**时需要的产物构建命令。空串＝无需 prepare
    （如 go run / npm start / python 直接跑源码自带构建或不需产物）。"""

    runtime_prepare_cmd_wrapper: str = ""
    """有 wrapper（如 `gradlew`）时使用的 prepare 命令。空串＝无 wrapper 变体。"""

    runtime_prepare_marker: str = ""
    """start_cmd 中代表"消费构建产物"的子串。只有 start_cmd 含此 marker 时才触发 prepare。
    例如 Maven 的 `target/*.jar`、Gradle 的 `build/libs/*.jar`。空串＝不触发。"""

    source_exclude_suffixes: tuple[str, ...] = field(default_factory=tuple)
    """判"参与编译源码"时要排除的后缀（如 `.d.ts` 只是类型声明，无编译产物）。"""

    layout_segments: tuple[str, ...] = field(default_factory=tuple)
    """该栈的**标准源码布局段**——它们是**布局**不是模块（`src/main/java` 的每段都不
    主张模块边界）。消费方取 `layout_segments_union()` 派生视图（P-M4，27 号文），
    绝不各抄一份（两份手抄必漂移，`_BUILD_MANIFESTS` 已实证两次）。"""

    workspace_container_segments: tuple[str, ...] = field(default_factory=tuple)
    """**workspace 容器段**（pnpm/turborepo 的 `packages/` `apps/`）：容器本身不是模块，
    容器+子目录（`packages/api`）才是模块根。与 layout_segments 是**两类**段：
    layout 段命中=【切在它前面】，容器段命中=【带上紧随的子目录一起算根】。
    混进 layout 表会让 `packages/api/src/x.ts` 在 i=0 处切出空根=塌模块（设计期实证）。

    ★position-0 判据★ 容器语义只在路径**首段**生效——深处的 `packages` 可能是
    普通包名（`mod/src/main/java/com/x/packages/Foo.java`），误判=把包目录当 workspace。"""


# ══════════════════════════════════════════════════════════════════
# 事实表（新增一栈 = 在此加一条，调用方零改动）
# ══════════════════════════════════════════════════════════════════

def gradle_build_cmd(project_dir: str = "", use_wrapper: bool = True) -> str:
    """#29-5 W-11：L1 派生闸用的【单入口】gradle 全量构建命令构造函数。

    两条血泪约束（29 号文 W-11 实测）：
    - 绝不 `||` 双跑：gradlew 真编译错（rc≠0）会触发回退分支再跑 gradle，机器无
      gradle 时输出尾部追加 `sh: 1: gradle: not found` ⇒ `_is_infra_failure` 把
      【真代码错】翻转成 infra 故障 ⇒ BLOCKED 走 transient 退避、repair 循环拿
      不到错误行零新增即 break ⇒ 空转到配额耗尽；
    - 绝不 `2>/dev/null`：stderr 是 L1 拿到的唯一编译错误证据源，吞掉=闸全盲。
    入口由调用方按 gradlew 存在性+可执行位（`_gradlew_executable`，沙箱优先）选定；
    project_dir 非空时按 W-4 收窄到子模块——-p 注在【选定入口】之后，命令里只有一个
    入口，不存在「正则只命中 `||` 第一分支」的旧缺陷。
    ★复核 LOW★ project_dir 传【裸】相对目录，函数内部 shlex.quote——「调用方已
    quote」的约定太脆弱（未来调用方传裸路径会产 `./gradlew -p my svc` 非法命令）。
    """
    import shlex as _shlex
    entry = "./gradlew" if use_wrapper else "gradle"
    p = f" -p {_shlex.quote(project_dir)}" if project_dir else ""
    return f"{entry}{p} -q classes"


STACK_SPEC: dict[str, StackSpec] = {
    "maven": StackSpec(
        key="maven", lang="java",
        root_manifests=("pom.xml",),
        module_manifest="pom.xml",
        aggregate_manifest="pom.xml", aggregate_field="<modules>",
        source_exts=(".java", ".kt", ".scala", ".groovy"),
        shares_classpath_namespace=True,
        has_aggregate_reconcile=True,
        # ★唯一有确定性 aggregator/模块脚手架 driver 的栈★（_AGGREGATOR_SCAFFOLD_STACKS）
        has_module_scaffold_driver=True,
        source_exclude_dirs=("target",),
        layout_segments=("src", "main", "java", "kotlin", "scala",
                         "resources", "test", "tests", "webapp"),
        whole_project_build_cmd="mvn -q -DskipTests compile",
        test_cmd="",
        runtime_start_cmd="java -jar target/*.jar",
        runtime_prepare_cmd="mvn -q -DskipTests package",
        runtime_prepare_marker="target/*.jar",
    ),
    "gradle": StackSpec(
        key="gradle", lang="java",
        root_manifests=("settings.gradle", "settings.gradle.kts",
                        "build.gradle", "build.gradle.kts"),
        module_manifest="build.gradle",
        aggregate_manifest="settings.gradle", aggregate_field="include(...)",
        aggregate_extra_manifests=("settings.gradle.kts",),
        module_extra_manifests=("build.gradle.kts",),
        source_exts=(".java", ".kt", ".scala", ".groovy"),
        shares_classpath_namespace=True,
        has_aggregate_reconcile=True,      # _reconcile_gradle（认 .kts）
        # 模块 build.gradle(.kts) 有 #31-P2f 脚手架 driver（P-H4c：坐标经 maven_registry
        # 原语解析——同坐标同仓库，BOM 受管省略版本）→ 模块清单 demote 安全
        has_module_scaffold_driver=True,
        source_exclude_dirs=("build",),
        layout_segments=("src", "main", "java", "kotlin", "scala",
                         "resources", "test", "tests", "webapp"),
        # ★#29-5 W-11★ 去掉 2>/dev/null（stderr 是 L1 唯一编译错误证据源，吞掉=
        # 闸全盲+输出只剩 not found ⇒ _is_infra_failure 翻转真代码错为 infra 故障）。
        # `|| gradle` 仅为 L2（integration_review 本地整跑）无 wrapper 工程留兜底；
        # L1 派生闸绝不用这串双跑形态——走 gradle_build_cmd() 构造单入口命令。
        whole_project_build_cmd="./gradlew -q classes || gradle -q classes",
        test_cmd="",
        runtime_start_cmd="java -jar build/libs/*.jar",
        runtime_prepare_cmd="gradle bootJar -x test -q",
        runtime_prepare_cmd_wrapper="./gradlew bootJar -x test -q",
        runtime_prepare_marker="build/libs/*.jar",
    ),
    "npm": StackSpec(
        key="npm", lang="node",
        root_manifests=("package.json",),
        module_manifest="package.json",
        aggregate_manifest="package.json", aggregate_field="workspaces",
        source_exts=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue"),
        # ★X-H3（27 号文 B-5）★ `_reconcile_npm` 已落地：workspaces【显式列表】
        # 形态三面（add/prune/probes）收编（glob 形态自愈不碰）；根 package.json
        # demote 的聚合档缺口闭合。模块档有 #31-P2b 脚手架 driver（owner 按契约
        # 一次建全+owner-backfill）→ 模块 package.json demote 安全（P-H4a 复核补翻——
        # driver 落地时漏翻，正是对账锁要逮的漂移）。
        has_aggregate_reconcile=True,
        # ★X-H3 R2★ _reconcile_npm 只补 workspaces 成员注册；demote 收回整文件写权
        # 时 scripts/dependencies 等编辑无兜底 → demote_safety_net 聚合档仍判不安全
        # （WARNING 照刷，文案说明注册有兜底、其它字段没有）。
        aggregate_reconcile_members_only=True,
        has_module_scaffold_driver=True,
        source_exclude_dirs=("node_modules", "dist", "build", "out", ".next"),
        source_exclude_suffixes=(".d.ts",),
        layout_segments=("src", "test", "tests"),
        # pnpm/turborepo workspace 容器（P-M4 主治：packages 布局塌模块）
        workspace_container_segments=("packages", "apps"),
        test_cmd="npm test --silent",
        test_priority=40,
        runtime_start_cmd="npm run start",
    ),
    "go": StackSpec(
        key="go", lang="go",
        root_manifests=("go.mod", "go.work"),
        module_manifest="go.mod",
        aggregate_manifest="go.work", aggregate_field="use(...)",
        source_exts=(".go",),
        has_aggregate_reconcile=True,      # _reconcile_go_work
        # 模块档有 #31-P2c 脚手架 driver（owner 按契约一次建全+owner-backfill）→
        # 模块 go.mod demote 安全（P-H4a 复核补翻，同 npm）。
        has_module_scaffold_driver=True,
        source_exclude_dirs=("vendor",),
        layout_segments=("cmd", "internal", "pkg"),
        whole_project_build_cmd="go build ./...",
        test_cmd="go test ./...",
        test_priority=20,
        runtime_start_cmd="go run .",
    ),
    "cargo": StackSpec(
        key="cargo", lang="rust",
        root_manifests=("Cargo.toml",),
        module_manifest="Cargo.toml",
        aggregate_manifest="Cargo.toml", aggregate_field="[workspace] members",
        source_exts=(".rs",),
        has_aggregate_reconcile=True,      # _reconcile_cargo（成员 Cargo.toml 无网）
        # 模块档有 #31-P2e 脚手架 driver（owner 按契约一次建全+owner-backfill，内部 crate
        # 物化 path 相对引用）→ 模块 Cargo.toml demote 安全（P-H4b）。
        has_module_scaffold_driver=True,
        source_exclude_dirs=("target",),
        layout_segments=("src", "tests"),
        whole_project_build_cmd="cargo build -q",
        test_cmd="cargo test -q",
        test_priority=30,
        runtime_start_cmd="cargo run",
    ),
    "python": StackSpec(
        key="python", lang="python",
        # `Pipfile` 是 M-1 合表时补的：旧 `integration_review` 的 if 链认它、本表原先不认
        # ⇒ 合表当场炸出 `test_supported_stacks_with_wider_manifests_are_not_misjudged`
        # （Pipfile 工程被误判 no_build_surface＝"闸未实现"）。这正是"两份实现必然漂移"
        # 的实证——合表之后这类漂移不可能再无声无息。
        root_manifests=("pyproject.toml", "setup.py", "requirements.txt", "Pipfile",
                        # ★B-7 准入闸（27 号文）★ `manage.py` 是 Django 工程的确定性证据，
                        # 一直在 `stack_detect._MANIFEST_BACKEND`（检测侧认它）却不在本表
                        # （driver 侧不认）→ 准入对账的唯一缺口。后果是实的：manage.py-only
                        # 工程在 `_detect_build_stack` 落 unknown→Maven 兜底造 pom、在
                        # detect_build_surface 落 NO_BUILD_SURFACE（"真没有"谎报）。
                        # 消费后果审计：①构建命令=compileall（Django 安全）②栈识别 python
                        # 优先于 unknown 兜底（正确方向）③plan 路径 manage.py 归 python（正确）。
                        "manage.py"),
        module_manifest="pyproject.toml",
        # ★P-H4a★ python per-pyproject driver 已落地（contract_utils `_P2_SCAFFOLD_DRIVERS`
        # 派生集对账，test_b3 防漂移）——owner 按契约一次建全模块清单，demote 安全。
        has_module_scaffold_driver=True,
        # ★刻意 None（诚实边界）★ poetry / uv / hatch 的 workspace 机制互不兼容，
        # 收录任何一种都是猜。缺席由 unregistered_aggregate_stacks() 机读可辨。
        aggregate_manifest=None, aggregate_field="",
        source_exts=(".py",),
        source_exclude_dirs=("build", "dist", ".venv", "site-packages"),
        layout_segments=("src", "test", "tests"),
        whole_project_build_cmd="python -m compileall -q .",
        test_cmd="python -m pytest -q --maxfail=1",
        test_priority=10,
    ),
}


# ══════════════════════════════════════════════════════════════════
# 派生视图（**只读**，绝不允许调用方另存一份）
# ══════════════════════════════════════════════════════════════════

def spec_for_stack(stack: str | None) -> StackSpec | None:
    """栈键 → spec。未收录栈返回 None（**绝不静默回退某个默认栈**）。"""
    if not stack:
        return None
    return STACK_SPEC.get(str(stack).strip().lower())


def root_aggregate_manifests() -> frozenset[str]:
    """全部已收录的**根聚合清单**名（含 `.kts` 别名）。

    消费契约＝"根级此文件必须单写者"（聚合结构重写非加性，双写者=rebase 循环根因）。
    ★判死的闸与收敛的 pass 必须都用这一个函数★——R-1 的两份名单就此合一。
    """
    out: set[str] = set()
    for spec in STACK_SPEC.values():
        if spec.aggregate_manifest:
            out.add(spec.aggregate_manifest)
            out.update(spec.aggregate_extra_manifests)
    return frozenset(out)


def root_manifests_by_stack() -> tuple[tuple[str, str], ...]:
    """全部已收录的**根清单** → [(规范大小写清单名, 栈键)]（确定性序：栈键序 + 表内序）。

    ★消费契约＝"磁盘上有这个文件 ⇒ 这是该栈的工程"（栈**识别**档）★ 与
    `root_aggregate_manifests`（聚合单写者档）、`structural_manifests`（demote 收敛档）
    **后果不同**，别互换：本档判错 = 栈识别错（P-C1 病灶：判 unknown → 兜底伪造 Maven）。

    ★为什么返回规范大小写★ 唯一消费场景是 `os.path.exists` 探测，而 Linux 大小写敏感，
    `Cargo.toml`/`Pipfile` 小写化后探不到（★F5 更正★：本档真正承重的大写清单只有这两条；
    原举例 `Gemfile` 属 ruby，**不在** STACK_SPEC——在 `integration_review.
    _UNSUPPORTED_STACK_MANIFESTS`＝刻意的未收录栈补集，它不经过本条路径）。
    plan 路径匹配请走 `stack_of_manifest`（内部
    `_plan_basename` 小写化，因为"LLM 写的路径大小写不可信"）——两档口径刻意不同。

    ★为什么是函数不是模块常量★ 与 `is_root_aggregate_manifest` 同款理由（复核 F-7）：
    冻结成 import 期常量会让"新增一栈只需加一条表项"的承诺在消费侧失效。
    """
    out: list[tuple[str, str]] = []
    for key in sorted(STACK_SPEC):
        for name in STACK_SPEC[key].root_manifests:
            out.append((name, key))
    return tuple(out)


def _plan_basename(path: str) -> str:
    """plan 路径 → **小写** basename。

    ★为什么小写★ 本函数只用于判 **LLM 写的 plan 路径**，而"LLM 写的路径大小写不可信"
    （contract_utils `_MANIFEST_TO_STACK_LC` 同款既有认知）。双复核实测：LLM 写小写
    `cargo.toml` 的根双写者既不吃判死闸、也不吃 demote → 非加性覆盖敞开（后写者抹掉
    前写者的 members）。**磁盘探测走 `root_manifests` 的规范大小写**（Linux 上
    `os.path.exists` 大小写敏感），两处口径刻意不同，别互换。
    """
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


def _lc(names) -> frozenset[str]:
    return frozenset(str(n).lower() for n in names)


def is_root_aggregate_manifest(path: str) -> bool:
    """归一化路径是否为**根级**聚合清单（无目录前缀）。子目录同名清单不算。

    ★消费者＝`plan_validator` 的根聚合硬失败闸（D1 backstop）★ 该闸**在调用时**读本函数，
    绝不在 import 期冻结成模块常量——冻结会让"新增一栈只需加一条表项"的承诺在闸侧失效
    （复核 F-7 实测：toy 栈进不了冻结集合，那条测试的前提句是被通用写者闸假过的）。
    """
    p = str(path or "").replace("\\", "/").lstrip("./").lstrip("/")
    return "/" not in p and p.lower() in _lc(root_aggregate_manifests())


def aggregate_manifests_of_stack(stack: str | None) -> tuple[str, ...]:
    """该栈**全部**聚合清单名（canonical 在首 + 别名）。未收录/无聚合 → 空元组。

    ★规则4 / 难度 bump / 一切"这是不是聚合清单"的判定都必须用本函数，不许只读
    `aggregate_manifest`★（复核 M-1/M-2/F-1：只读单数字段 → `.kts` 别名整列落空）。
    canonical 在首＝多个候选同时存在于磁盘时的确定性选择序。
    """
    spec = spec_for_stack(stack)
    if not spec or not spec.aggregate_manifest:
        return ()
    return (spec.aggregate_manifest, *spec.aggregate_extra_manifests)


def module_manifests_of_stack(stack: str | None) -> tuple[str, ...]:
    """该栈**全部**模块清单名（canonical 在首 + 别名）。未收录 → 空元组。"""
    spec = spec_for_stack(stack)
    if not spec:
        return ()
    return (spec.module_manifest, *spec.module_extra_manifests)


def structural_manifests() -> frozenset[str]:
    """【结构性全文件】构建清单 basename（根或模块）。

    消费契约（与 `root_aggregate_manifests` **不同档**，别混用）＝
    "同一个此文件的多写者必须收敛唯一 owner，非首写者 demote 为 readable + 依赖 owner"。
    理由：pom/gradle/json/toml 都是**整段结构重写**的文档，两个写者各自重写
    `<modules>` / `dependencies` / `[workspace]` / `workspaces`，union/3-way 合并
    无法收口（round18 P0-A 根 pom 畸形闭标签 / round19 模块 pom 双 `<project>` 拼接）。

    ★为什么不是"所有栈的所有清单"（诚实边界·后果分档）★
    demote 会让非 owner 写者失去写权，**只有在登记有确定性补回路径时才安全**：
      · maven/gradle/cargo/go —— `worker/workspace_manifest.py` 有对应 `_reconcile_*`，
        据磁盘 ground-truth 补齐注册（L1/L2/交付三处），再加规则4 的 owner 显式登记＝双保险；
      · npm —— **无 `_reconcile_npm`**，只剩规则4 的 owner 登记这**一道**网（已登记为
        B-5 ManifestDriver 待补项，不假装双保险）；
      · python —— `aggregate_manifest is None`（workspace 机制生态碎片化，未收录）＝
        **根档既无 reconcile 也无规则4 登记** → 根 pyproject.toml demote 必丢贡献。
        模块档虽已（P-H4a）有确定性脚手架 driver（`has_module_scaffold_driver=True`，
        新建撞车 demote 安全），但本集合是 basename 判据、**无法区分根/模块
        pyproject.toml**——收录会把根档也拖进 demote → **刻意不收录本集合**，
        双写者维持"串行化保留写权"（比 demote 丢贡献安全）。
    """
    out: set[str] = set()
    for spec in STACK_SPEC.values():
        if not spec.aggregate_manifest:
            continue                      # 无聚合登记路径的栈 → 不做 demote 式收敛
        out.add(spec.aggregate_manifest)
        out.update(spec.aggregate_extra_manifests)
        out.add(spec.module_manifest)
        out.update(spec.module_extra_manifests)
    return frozenset(out)


def build_manifest_basenames() -> frozenset[str]:
    """【实现证据排除档】全栈构建清单 basename 并集（第四档派生视图，P-C1 复核 F1）。

    消费契约（与另三档**不同档**，别互换）＝
    "该 basename 是构建清单本身 ⇒ 写它**不构成『实现了接口/符号』的证据**"。
    消费者：符号挂靠候选过滤（`symbol_surgery._subtask_modules`）、物理根证据分类
    （`contract_utils._evidence_class`）——问的都是"这个文件能不能作证"，不是
    "要不要 demote"（`structural_manifests`）也不是"这是不是该栈的工程"（`root_manifests`）。

    ★为什么不能用 `structural_manifests()`（血规 10 第三条正例）★ 那档有
    `aggregate_manifest` 门控（python 刻意不收录——demote 必丢贡献）。但本档问的是
    另一件事：清单永远不是实现。python 的 `pyproject.toml`/`requirements.txt` 不在
    本档 ⇒ 只写 `backend/pyproject.toml` 的纯脚手架子任务照旧是符号挂靠候选
    ⇒ 幻影 ownership 骗过 C1、两张皮复活（F1 实测 `mod/go.mod`/`mod/Cargo.toml`/
    `mod/settings.gradle.kts`/`mod/pyproject.toml` 全部返 `{'mod': 1}`）。

    ★方向性：本档宁滥勿缺★ 多收一个只会让某子任务少一个挂靠候选（保守方向，
    缺席 ⇒ 不挂 ⇒ C1 打回，fail-closed）；少收一个直接造幻影 ownership（静默放行）。
    与 `root_manifests_by_stack`（少收＝栈识别错）方向相反，故绝不共用。

    定义＝全栈并集：`root_manifests` ∪ `module_manifest` ∪ `module_extra_manifests`
    ∪ `aggregate_manifest` ∪ `aggregate_extra_manifests`，**无任何门控**。
    """
    out: set[str] = set()
    for spec in STACK_SPEC.values():
        out.update(spec.root_manifests)
        out.add(spec.module_manifest)
        out.update(spec.module_extra_manifests)
        if spec.aggregate_manifest:
            out.add(spec.aggregate_manifest)
            out.update(spec.aggregate_extra_manifests)
    return frozenset(out)


def is_structural_build_manifest(path: str) -> bool:
    """路径（根或任意嵌套模块）是否为结构性构建清单。见 `structural_manifests` 的消费契约。"""
    return _plan_basename(path) in _lc(structural_manifests())


def stack_of_structural_manifest(name: str) -> str | None:
    """结构性清单 basename → 栈键（聚合或模块清单皆可，**大小写不敏感**）。

    ★与 `stack_of_manifest` 是两档★：本函数答"这个清单属于哪个栈的结构体系"（`go.work`
    答 go、`settings.gradle.kts` 答 gradle）；`stack_of_manifest` 答"根上有这个文件说明
    这是哪个栈的工程"（按 `root_manifests`，`go.work`/`Cargo.toml` 两者都能答，但
    `settings.gradle.kts` 只有前者收录）。demote 留痕要的是**前者**——它对聚合/模块
    别名全覆盖，不会因为清单不在 `root_manifests` 里就把栈判成 None 而静默跳过留痕。
    """
    n = _plan_basename(name)
    for spec in STACK_SPEC.values():
        if n in _lc(aggregate_manifests_of_stack(spec.key)) or \
                n in _lc(module_manifests_of_stack(spec.key)):
            return spec.key
    return None


def demote_safety_net(path: str, stack: str | None) -> tuple[bool, str]:
    """demote 掉该清单时**有没有兜底网** → (safe, tier)。tier ∈ {aggregate, module}。

    ★两档不可互换（复核 M-3：这正是本仓"复用单一事实源 ≠ 复用其消费契约"的复发）★
      · 根级（无目录前缀）→ 聚合档，看 `has_aggregate_reconcile`：`_reconcile_*` 据磁盘
        ground-truth 补回**注册**；★X-H3 R2★ 但 `aggregate_reconcile_members_only`
        （npm）的 reconcile 只补成员注册——整文件其它字段（scripts/dependencies…）的
        demote 编辑补不回，聚合档仍判【不安全】（WARNING 照刷）；
      · 模块级 → 模块档，看 `has_module_scaffold_driver`：owner 按契约一次建全模块清单，
        非 owner 本无合法贡献（#11a doctrine），demote 无损。
    把聚合档的 reconcile 事实当"该栈任何清单都有网"用，会让 gradle 的模块清单
    丢真实编辑（该子任务想加的依赖）时连一句 WARNING 都没有（M-3 原话 go/gradle/cargo，
    go/cargo 已由 #31-P2c/2e driver 补齐）。
    """
    spec = spec_for_stack(stack)
    p = str(path or "").replace("\\", "/").lstrip("./").lstrip("/")
    tier = "aggregate" if "/" not in p else "module"
    if spec is None:
        return False, tier
    if tier == "aggregate":
        # ★X-H3 R2★ members_only 档（npm）：reconcile 只补成员注册，整文件其它
        # 字段（scripts/dependencies…）的 demote 编辑无兜底 → 判不安全（WARNING 照刷）。
        safe = (spec.has_aggregate_reconcile
                and not spec.aggregate_reconcile_members_only)
    else:
        safe = spec.has_module_scaffold_driver
    return safe, tier


def stack_of_manifest(name: str) -> str | None:
    """任意清单 basename → 栈键（按 root_manifests 判"这是哪个栈的工程"）。

    ★与 `stack_of_structural_manifest` 是两档★：本函数答"这是哪个栈"，那个答"这个清单
    属于谁的结构体系"。`build.gradle` 两者都答 gradle，但 `settings.gradle.kts` 只有
    root_manifests 收录时才被本函数认出——判 demote 留痕请用那个，别用本函数。
    """
    n = _plan_basename(name)
    for spec in STACK_SPEC.values():
        if n in _lc(spec.root_manifests):
            return spec.key
    return None


def aggregate_manifest_of_stack(stack: str | None) -> str | None:
    spec = spec_for_stack(stack)
    return spec.aggregate_manifest if spec else None


def module_manifest_of_stack(stack: str | None) -> str | None:
    spec = spec_for_stack(stack)
    return spec.module_manifest if spec else None


DEPENDENCY_TREE_DIRS: frozenset[str] = frozenset({
    # ★这里只放【第三方代码躺的树】★ 判据："这个目录里的代码不是本仓写的，也不由本仓的版本
    # 约定管"。vendored 依赖、包管理器缓存、虚拟环境、语言 runtime 的第三方落点。
    "node_modules", "vendor", "third_party", "third-party", "3rdparty",
    ".yarn", ".pnpm-store", "bower_components", "Pods", "packages_cache",
    ".tox", ".eggs", "site-packages", ".venv", "venv",
})
"""**依赖树**目录（第三方代码），与"产物/工具目录"（`target`/`build`/`dist`/`.gradle`…）刻意分表。

★为什么必须分（"复用单一事实源 ≠ 复用其消费契约"）★
`sandbox_spec._SKIP_DIRS` = 依赖树 ∪ 产物/工具，它答的是"**哪个清单算构建入口**"——产物目录里
的清单不是入口，所以两类都该跳。但另一类消费者问的是**别的问题**："这个目录里的模块声明，能不能
用来推**兄弟模块**的约定？"（N-2b 的 go module 前缀取证）。对这个问题：
  · 依赖树目录 → **不能**（`vendor/x` 的 module 路径是 `github.com/third/x`，第三方的命名约定
    与本仓无关，拿它推兄弟前缀必然错）；
  · 产物/工具目录 → 与本问题无关。`build/tool/go.mod` 若真存在，它是**本仓自己的**模块（monorepo
    里把工具放 `build/` 并不违法），它的 module 路径照样是本仓约定的证据。拿 `_SKIP_DIRS` 整张表
    去过滤，就把这类合法证据也剔了 → 前缀推不出 → 整栈零脚手架（**误杀**，比不治更坏）。

所以：`_SKIP_DIRS` 由本集合 ∪ 产物集合**组合**而成（单一事实源不破），而前缀取证这类
"谁的声明可信"的消费者只读本集合。改本表前先问：新消费者的后果和老消费者一样吗？
"""


def unregistered_aggregate_stacks() -> tuple[str, ...]:
    """聚合机制**未收录**的栈键——缺席必须机读可辨（纪律：`return []` 与"真没有"不可分
    时，那一层可以死很久没人知道）。有消费者：见 test 的两表对账与 B-7 的覆盖面登记。"""
    return tuple(sorted(s.key for s in STACK_SPEC.values() if not s.aggregate_manifest))


def layout_segments_union() -> frozenset[str]:
    """全部已收录栈的**标准源码布局段**并集（P-M4，27 号文）。

    ★消费契约＝"栈无关判定"档★：调用方（证据分层/模块根切分）此刻**不知道**文件属于
    哪个栈，用并集保守判定——某段在任一栈里是布局，就不让它主张模块边界。
    与旧手抄 12 段 frozenset 逐元素相等（测试锁 `test_layout_segments_union_equals_legacy`）。
    """
    return frozenset(seg for s in STACK_SPEC.values() for seg in s.layout_segments)


def workspace_container_segments_union() -> frozenset[str]:
    """全部已收录栈的 **workspace 容器段**并集（pnpm/turborepo 的 `packages`/`apps`）。

    ★消费契约＝position-0★：只在路径**首段**生效，且容器本身不是模块——
    `容器/子目录` 才是模块根。深处的同名段（Java 包名 `…/com/x/packages/`）绝不命中。
    """
    return frozenset(
        seg for s in STACK_SPEC.values() for seg in s.workspace_container_segments)


def is_compilable_source(path: str, stack: str | None) -> bool:
    """该文件是否为该栈的**参与编译源码**（资源/清单/文档/产物/vendored 皆 False）。

    栈未知 → False（fail-closed）。

    ★测试文件刻意【计入】★：`a_test.go` / `a.spec.ts` 是人手写的、要过编译的，
    "一次写 3 个测试文件"确实是多步工作 —— 难度路由要的正是这个信号。
    排除的只有**从不由人手写**的东西：vendored 目录、构建产物目录、纯类型声明
    （`.d.ts` 无编译产物）。误计的方向是过度提难度＝白占算力（R62-Task6 点过的路由异味）。
    """
    spec = spec_for_stack(stack)
    if not spec:
        return False
    p = str(path or "").replace("\\", "/").lstrip("./").lower()
    if any(seg in spec.source_exclude_dirs for seg in p.split("/")[:-1]):
        return False
    if spec.source_exclude_suffixes and p.endswith(
            tuple(e.lower() for e in spec.source_exclude_suffixes)):
        return False
    return p.endswith(tuple(e.lower() for e in spec.source_exts))
