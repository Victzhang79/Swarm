#!/usr/bin/env python3
"""B-0 共享 workspace 夹具 × 四消费者 —— 矩阵回归（27 号文 §7 B-0）。

## 这个文件存在的理由

27 号文 §4.3 实测非 Maven 侧**零回归网**，结论原文：

> 把 `pom.xml` 特判改成通用清单表、或调整栈优先级，大概率一条测试都不会变红。
> 回归只会在下一次 live 跑非 Java 项目时暴露——这正是 L2「休眠债」的定义。**所以红灯必须先行。**

本文件把 `conftest` 的四型（+single-root +Maven 对照臂）夹具接到**真实消费者**上：

| # | 消费者 | 断言什么 |
|---|---|---|
| ① | `stacks.STACK_SPEC` | 夹具与事实表**不漂移**（夹具说自己是 go.work 工程，表也得这么认） |
| ② | `stacks.is_compilable_source` | 源码全收、噪声全排（vendored/产物/`.d.ts`） |
| ③ | `worker.workspace_manifest` | 未登记成员被补回 —— **按 spec 声明的档位**，npm 无网就得如实无网 |
| ④ | `project.sandbox_spec` | 每型都推得出工具链（镜像里装对东西） |
| ⑤ | `worker.l1_pipeline` | 每型都派生得出全量构建命令（否则 L1 闸恒 skip=恒通过） |
| ⑥ | `brain.plan_validator` + 规则1/4 | **R-1 收敛闭环**在真实磁盘树上成立（判死的必须能被收敛） |

## ★夹具落地当场抓到的两条新红灯（strict xfail，不是"待办注释"）★

- **N-1** `sandbox_spec._infer_npm` 只读**根** `package.json` 的 scripts。npm workspaces
  的根常常只有 `{name,private,workspaces}`（构建脚本在各子包）→ `toolchains=[]` →
  **镜像里根本没装 node** → 任何 npm 命令 127 → BLOCKED 无限退避。与 X-C2（Gradle 工程
  镜像没装 gradle）**同型**，只是换了个栈。
- **N-2** `l1_pipeline._derive_full_build_command` 的清单探测**只看工程根**，而
  `_BUILD_TOOL_MANIFESTS["go"]` 只有 `go.mod`、没有 `go.work`。go.work 多模块仓根上
  **没有** `go.mod` → `has("go.mod")=False` → 返回 `''` → **零构建闸**。实测唯一还能
  救它的是 `project_stack.build == "go"`，而那个键是 **LLM 可写的自由文本**（§2.1 未闭合
  的 L6 复发面）——也就是说**确定性证据路径整条缺席**。

两条都归 B-4/B-5（BuildDriver / SandboxDriver），本批**只上红灯不修**——B-0 的职责是
把网织出来。`xfail(strict=True)`：修好那天变 XPASS → 测试失败 → 逼人回来摘标记。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec_bs = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod_bs = importlib.util.module_from_spec(_spec_bs)
_spec_bs.loader.exec_module(_mod_bs)

# 夹具 builders（conftest 已注册进 sys.modules；此处按路径独立加载，不依赖加载序）。
_sw = Path(__file__).resolve().parent / "stack_workspaces.py"
_spec_sw = importlib.util.spec_from_file_location("stack_workspaces", _sw)
sw = importlib.util.module_from_spec(_spec_sw)
_spec_sw.loader.exec_module(sw)

from swarm.stacks import (  # noqa: E402
    STACK_SPEC,
    aggregate_manifests_of_stack,
    demote_safety_net,
    is_compilable_source,
    module_manifests_of_stack,
    spec_for_stack,
)

ALL = tuple(sw.WORKSPACE_BUILDERS)
AGG = sw.AGGREGATE_WORKSPACES


@pytest.fixture
def fx(request, tmp_path):
    """按 `@pytest.mark.parametrize("fx", [...], indirect=True)` 造树。"""
    return sw.build_workspace(request.param, tmp_path / request.param)


# ══════════════════════════════════════════════
# ① 夹具 ↔ 事实表 不漂移
# ══════════════════════════════════════════════

@pytest.mark.parametrize("fx", ALL, indirect=True)
def test_fixture_agrees_with_the_fact_table(fx):
    """夹具自称的栈/语言/聚合清单必须与 `STACK_SPEC` 逐字一致。

    ★为什么这条必须第一★ 夹具若自称"go.work 工程"而事实表不这么认，后面所有断言都在
    测**别的东西**——这正是本仓"夹具让测试走不进被测分支"那类假绿的源头。本条红了说明
    要么夹具写错，要么有人改了表却没跟夹具对账。
    """
    spec = spec_for_stack(fx.stack)
    assert spec is not None, f"{fx.name}: 夹具栈 {fx.stack!r} 不在 STACK_SPEC"
    assert spec.lang == fx.lang, f"{fx.name}: lang 漂移 spec={spec.lang} 夹具={fx.lang}"

    if fx.aggregate_manifest is None:
        # single-root：该栈**有**聚合机制（go 有 go.work），只是这棵树没用它
        assert fx.topology == "single-root", (
            f"{fx.name}: 无聚合清单却不是 single-root 拓扑（拓扑与事实必须自洽）")
        assert not fx.module_manifests, "single-root 仓不该有 per-module 清单（L5）"
        return

    aggs = aggregate_manifests_of_stack(fx.stack)
    assert fx.aggregate_manifest in aggs, (
        f"{fx.name}: 聚合清单 {fx.aggregate_manifest} 不在该栈全集 {aggs}"
        "（别名字段漏了？F-1 就是这条）")
    mods = module_manifests_of_stack(fx.stack)
    for mm in fx.module_manifests:
        assert Path(mm).name in mods, f"{fx.name}: 模块清单 {mm} 不在该栈全集 {mods}"


def test_unknown_fixture_name_fails_loudly():
    """夹具工厂对未知名必须**大声失败**，绝不静默回退某一型。

    ★为什么这条也要有锁★ 本批教训4：突变清单要对齐**改动清单**，不是 findings 清单。
    `build_workspace` 的 fail-loud 是我这批新造的原语——若它悄悄回退到某个默认型，
    parametrize 里写错一个名字就会变成"同一型跑了两遍"的假绿，而计数看起来毫无异常。
    """
    with pytest.raises(KeyError, match="未知 workspace 夹具"):
        sw.build_workspace("nope_not_a_stack", Path("/tmp/does-not-matter-b0"))


@pytest.mark.parametrize("fx", ALL, indirect=True)
def test_fixture_tree_is_on_disk_as_declared(fx):
    """夹具声明的每个路径都真在磁盘上 —— 声明与树不符=后续断言全部失去意义。"""
    declared = (list(fx.module_manifests) + list(fx.sources) + list(fx.noise)
                + ([fx.aggregate_manifest] if fx.aggregate_manifest else []))
    missing = [p for p in declared if not (fx.root / p).is_file()]
    assert not missing, f"{fx.name}: 声明了但磁盘上没有：{missing}"
    for d in list(fx.registered) + list(fx.unregistered):
        assert (fx.root / d).is_dir(), f"{fx.name}: 模块目录 {d} 不存在"


# ══════════════════════════════════════════════
# ② 源码判据：源码全收、噪声全排
# ══════════════════════════════════════════════

@pytest.mark.parametrize("fx", ALL, indirect=True)
def test_compilable_source_covers_sources_and_excludes_noise(fx):
    """`is_compilable_source` 对每型：声明的源码全 True、噪声全 False。

    噪声不是凑数——`node_modules/` `target/` `build/` `vendor/` `dist/` `.d.ts` 每一条都
    对应 `source_exclude_dirs` / `source_exclude_suffixes` 里的一格。删掉任一格，这条红。
    """
    missed = [s for s in fx.sources if not is_compilable_source(s, fx.stack)]
    assert not missed, f"{fx.name}: 源码被漏判（难度路由/同名判据会失效）：{missed}"
    leaked = [n for n in fx.noise if is_compilable_source(n, fx.stack)]
    assert not leaked, f"{fx.name}: 噪声被当源码（过度提难度=白占算力）：{leaked}"


@pytest.mark.parametrize("fx", ALL, indirect=True)
def test_multi_source_signal_is_not_jvm_gated(fx):
    """R-3 回归（矩阵档）：**每型**都必须数得出"这个子任务写了几个源码文件"。

    R-3 原病：`many_sources` 走 `classpath_fqn_key`（JVM 门控，对 `.ts`/`.go`/`.rs` 恒
    None）→ 非 JVM 栈判 0 个源码文件 → 难度永不提升 → R67-T9 这条治本**对所有非 JVM 栈
    从未生效过**。这里直接数夹具的源码集：任何一型数出 0 就是门控回来了。
    """
    n = sum(1 for s in fx.sources if is_compilable_source(s, fx.stack))
    assert n >= 2, (
        f"{fx.name}: 只数出 {n} 个源码文件（夹具有 {len(fx.sources)} 个）"
        "——JVM 门控回归？R-3 就是这么对非 JVM 栈全程失效的")


# ══════════════════════════════════════════════
# ③ workspace_manifest：未登记成员按【spec 声明的档位】补回
# ══════════════════════════════════════════════

@pytest.mark.parametrize("fx", AGG, indirect=True)
def test_reconcile_adds_unregistered_members_per_declared_tier(fx):
    """磁盘上有、聚合清单里缺的成员 → `_reconcile_*` 该补的补、该不补的**如实不补**。

    ★分支判据取自 `spec.has_aggregate_reconcile`，不是我手写的期望★——那个字段由
    `test_reconcile_facts_match_reality` 独立对 `workspace_manifest` 的真实函数集对账，
    所以这里不构成自证循环：谁将来写了 `_reconcile_npm` 却没翻 spec，先在那条红。

    npm 分支断言的是**诚实缺口**（spec 写 False=真无网）：不假装它有网。B-5 补上
    `_reconcile_npm` 那天，那条对账测试会红，人被逼回来把这里的分支一起改。
    """
    from swarm.worker.workspace_manifest import reconcile_workspace_manifests

    spec = spec_for_stack(fx.stack)
    r = reconcile_workspace_manifests(str(fx.root))
    added = {k: v for k, v in (r.get("added") or {}).items()}
    agg_text = (fx.root / fx.aggregate_manifest).read_text(encoding="utf-8")

    if spec.has_aggregate_reconcile:
        assert fx.aggregate_manifest in added, (
            f"{fx.name}: spec 声明有聚合 reconcile，却没补 {fx.unregistered}；"
            f"实得 added={added} modified={r.get('modified_manifests')}")
        for d in fx.unregistered:
            leaf = Path(d).name
            assert leaf in agg_text or d in agg_text, (
                f"{fx.name}: {d} 未写进 {fx.aggregate_manifest}:\n{agg_text}")
        # 幂等（对账器的硬契约：跑两遍不得再动）
        r2 = reconcile_workspace_manifests(str(fx.root))
        assert r2["modified_manifests"] == [], f"{fx.name}: 非幂等，二次跑又改了 {r2}"
    else:
        assert fx.stack == "npm", (
            f"{fx.name}: 除 npm 外不该有【无聚合 reconcile】的已收录栈（表变了？）")
        assert fx.aggregate_manifest not in added, (
            f"{fx.name}: spec 说无 reconcile 却补了 {added}——事实表与实现漂移")
        for d in fx.unregistered:
            assert d not in agg_text, (
                f"{fx.name}: 意外补进了 {d}，spec 该翻 True 了")


@pytest.mark.parametrize("fx", AGG, indirect=True)
def test_demote_safety_net_is_tiered_on_a_real_tree(fx):
    """M-3 回归（矩阵档）：同一棵真实树上，**根档与模块档的兜底结论必须能不同**。

    原病：`has_manifest_reconcile` 一个布尔被消费成"该栈任何清单 demote 都有兜底" →
    go/gradle/cargo 的**模块**清单丢真实编辑连 WARNING 都没有。这里逐型验两档各自读
    各自的事实源；`maven` 是唯一两档皆 True 的栈，正好当对照。
    """
    spec = spec_for_stack(fx.stack)
    safe_agg, tier_agg = demote_safety_net(fx.aggregate_manifest, fx.stack)
    assert tier_agg == "aggregate", f"{fx.name}: 根清单档位判错 {tier_agg}"
    assert safe_agg is spec.has_aggregate_reconcile

    if fx.module_manifests:
        mm = fx.module_manifests[0]
        safe_mod, tier_mod = demote_safety_net(mm, fx.stack)
        assert tier_mod == "module", f"{fx.name}: {mm} 档位判错 {tier_mod}"
        assert safe_mod is spec.has_module_scaffold_driver
        if fx.stack != "maven":
            assert safe_mod is False, (
                f"{fx.name}: 非 maven 栈的模块清单不该有脚手架 driver 兜底"
                "（有了就得给 spec 翻 True，否则白刷告警）")


def test_reconcile_never_fabricates_an_aggregate_for_single_root(make_workspace):
    """L5 回归：单根仓**绝不**被擅自造出聚合清单。

    L5 原病：拿 Maven reactor 当所有栈的模块观、把每个顶层目录当独立构建单元 → Go 单根/
    Rust 单 crate 的补丁被静默丢弃 → abandoned → PARTIAL。`_reconcile_go_work` 的注释
    也写明"绝不创建 go.work（单模块库无须工作区，擅自建会改变构建语义）"。
    """
    from swarm.worker.workspace_manifest import reconcile_workspace_manifests

    fx = make_workspace("go_single_root")
    r = reconcile_workspace_manifests(str(fx.root))
    assert not (fx.root / "go.work").exists(), "擅自给单根仓造了 go.work（改变构建语义）"
    assert r["modified_manifests"] == [], f"单根仓不该有任何聚合改动，实得 {r}"
    # cmd/ internal/ 是包目录，不是模块——不该被塞 per-module 清单
    assert not (fx.root / "cmd" / "serve" / "go.mod").exists()
    assert not (fx.root / "internal" / "store" / "go.mod").exists()


# ══════════════════════════════════════════════
# ④ sandbox_spec：每型都得推出工具链（否则镜像里没那个工具）
# ══════════════════════════════════════════════

_EXPECTED_TOOLCHAIN = {
    "maven": "java", "gradle": "java", "npm": "node",
    "go": "go", "cargo": "rust", "python": "python",
}


def test_expected_toolchain_table_covers_every_registered_stack():
    """准入闸（B-7 的雏形）：新增一栈必须在本表有期望值，不许悄悄漏出矩阵。"""
    missing = sorted(set(STACK_SPEC) - set(_EXPECTED_TOOLCHAIN))
    assert not missing, f"新栈 {missing} 未登记期望工具链——矩阵会静默漏掉它"


@pytest.mark.parametrize("fx", ALL, indirect=True)
def test_sandbox_spec_infers_a_toolchain_for_every_stack(fx, request):
    """镜像层：每型工程都必须推出对应工具链。

    ★N-1 红灯（strict xfail）★ `_infer_npm` 只读**根** package.json 的 scripts，而 npm
    workspaces 的根常常只有 `{name,private,workspaces}`（构建脚本在各子包）→ `toolchains=[]`
    → **镜像里没装 node** → 任何 npm 命令 127 → BLOCKED 无限退避。与 X-C2（Gradle 工程
    镜像没装 gradle）同型。归 B-5 SandboxDriver。
    """
    from swarm.project.sandbox_spec import find_build_files, infer_env_spec

    if fx.name == "npm_workspaces":
        request.node.add_marker(pytest.mark.xfail(
            strict=True, reason="N-1（B-5）：_infer_npm 只看根 package.json 的 scripts，"
                                "workspaces 根无 scripts → 零 node 工具链 → 镜像没装 node"))

    bf = find_build_files(fx.root)
    assert bf, f"{fx.name}: 一个构建文件都没发现"
    env = infer_env_spec(str(fx.root), project_id="p")
    names = {t.name for t in env.toolchains}
    want = _EXPECTED_TOOLCHAIN[fx.stack]
    assert want in names, (
        f"{fx.name}: 期望工具链 {want}，实得 {names or '空'}"
        f"（构建文件已发现 {sorted(bf)}）——镜像里不会装它，命令必 127")
    tc = next(t for t in env.toolchains if t.name == want)
    assert tc.dep_source and (fx.root / tc.dep_source).is_file(), (
        f"{fx.name}: dep_source={tc.dep_source!r} 不指向真实文件")


# ══════════════════════════════════════════════
# ⑤ l1_pipeline：每型都派生得出全量构建命令（否则 L1 闸恒 skip = 恒通过）
# ══════════════════════════════════════════════

@pytest.mark.parametrize("fx", ALL, indirect=True)
def test_full_build_command_derives_from_disk_evidence_alone(fx, request):
    """L1 闸：改了源码就必须派生出该栈的**全量构建**命令，且只凭**磁盘证据**。

    ★刻意传 `project_stack=None`★ 那个画像的 `build` 键是 **LLM 可写的自由文本**
    （27 号文 §2.1，L6 未闭合的复发面）——LLM 写成 `"Java/Maven"` 就正则解不出。所以
    "有画像才行"等于没有确定性保障。这里只给磁盘。

    ★N-2 红灯（strict xfail）★ go.work 多模块仓根上没有 `go.mod`，而清单探测**只看工程
    根**、`_BUILD_TOOL_MANIFESTS["go"]` 也没收 `go.work` → 返回 `''` → **零构建闸**
    （X-H2 同款"跳过=通过"）。归 B-5 BuildDriver。
    """
    from swarm.worker.l1_pipeline import _build_cmd_applicable, _derive_full_build_command

    if fx.name == "go_work":
        request.node.add_marker(pytest.mark.xfail(
            strict=True, reason="N-2（B-5）：清单探测只看工程根 + _BUILD_TOOL_MANIFESTS 无 "
                                "go.work → go.work 多模块仓零构建闸"))

    for src in fx.sources:
        cmd = _derive_full_build_command(str(fx.root), [src], None)
        assert cmd, (
            f"{fx.name}: 改 {src} 派生不出构建命令 → L1 构建闸整条跳过"
            "（'跳过=通过'，坏产物直接放行）")
        assert _build_cmd_applicable(cmd, str(fx.root)), (
            f"{fx.name}: 派生出 {cmd!r} 却判不适用 → 闸仍旧跳过")


def test_go_work_gap_is_the_manifest_table_not_the_derivation(make_workspace):
    """N-2 定位（**不修，只钉死根因**）：同一棵 go.work 树，给了画像就派生得出。

    这条把"派生逻辑坏了"与"确定性证据源缺 go.work"两种可能分开——是后者。B-5 要改的是
    清单表/探测面（让 `go.work` 也算 go 工程的证据），不是 `.go` 分支的派生逻辑。
    """
    from swarm.worker.l1_pipeline import _BUILD_TOOL_MANIFESTS, _derive_full_build_command

    fx = make_workspace("go_work")
    assert _derive_full_build_command(str(fx.root), ["auth/token.go"], None) == "", (
        "N-2 已被修好 → 请摘掉上面那条 xfail(strict) 并把本测试翻成正向断言")
    assert _derive_full_build_command(
        str(fx.root), ["auth/token.go"], {"build": "go"}) == "go build ./...", (
        "派生逻辑本身也坏了（不只是证据源缺 go.work）——B-5 范围要扩")
    assert "go.work" not in (_BUILD_TOOL_MANIFESTS.get("go") or ()), (
        "go.work 已进清单表 → N-2 的一半已治，请同步更新本测试")


# ══════════════════════════════════════════════
# ⑥ R-1 收敛闭环：判死的必须能被收敛（真实磁盘树档）
# ══════════════════════════════════════════════

@pytest.mark.parametrize("fx", AGG, indirect=True)
def test_r1_condemned_aggregate_writers_converge_on_a_real_tree(fx):
    """★R-1 本尊的矩阵档★ 两个子任务都写根聚合清单 → 收敛器必须当场收敛成单写者。

    R-1 原病：判死的名单（`plan_validator` 硬闸）⊅ 收敛的名单（规则1/规则4）→
    `go.work`/`settings.gradle`/`Cargo.toml` **判死却无人收敛** → 规划期硬闸永不收敛 →
    同签名连续两轮 → **熔断 fail-fast** → 那三栈的多模块工程 100% 死在规划期。

    与 `test_b3_stack_spec_single_source.py` 的差别：那边用合成 plan（`_exists_in_repo`
    打桩），这边跑在**真实磁盘树**上——规则4 的 owner 探测、聚合-vs-新建分流都读真磁盘，
    夹具让这些取证走真实路径（"非 git 目录测 git 路径"那类假绿的反面）。
    """
    from swarm.brain.contract_utils import resolve_plan_conflicts
    from swarm.brain.plan_validator import validate_plan_structure
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    agg = fx.aggregate_manifest
    new_mod = fx.unregistered[0] if fx.unregistered else "extra"
    mm = Path(fx.module_manifests[0]).name if fx.module_manifests else "manifest"
    # ★刻意让 st-2 依赖 st-1（已串行化）★ 通用"并行同写"闸此时**不响**，只有根聚合硬闸会响
    # ——这才是有区分力的夹具。上一批 F-7 的假绿正是"toy 栈进不了判死名单，但通用写者闸
    # 照样把 plan 判 invalid"，于是 `not valid` 对名单漏判毫无区分力（本批突变 M10 当场
    # 重演：把 package.json 从名单里摘掉，我原来那条断言仍全绿）。
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description=f"建 {new_mod} 并注册进 {agg}",
                difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=[f"{new_mod}/{mm}"], writable=[agg]),
                acceptance_criteria=["ok"]),
        SubTask(id="st-2", description=f"建 second 并注册进 {agg}",
                difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=[f"second/{mm}"], writable=[agg]),
                depends_on=["st-1"], acceptance_criteria=["ok"]),
    ], parallel_groups=[["st-1"], ["st-2"]], shared_contract={})

    before = validate_plan_structure(plan)
    assert not before.valid, (
        f"{fx.name}: 根聚合清单 {agg} 的双写者竟未被判死——判死名单漏了它"
        f"（R-1 的 package.json 侧就是这个洞）；issues={before.issues}")
    assert any(f"根聚合清单 {agg}" in i for i in before.issues), (
        f"{fx.name}: 判死了但**不是根聚合闸**判的（通用写者闸假过=F-7 那类）。"
        f"名单里没有 {agg}？issues={before.issues}")

    resolve_plan_conflicts(plan, project_path=str(fx.root), base_ref=None)

    after = validate_plan_structure(plan)
    assert after.valid, (
        f"{fx.name}: 判死后收敛器没收住 → 规划期硬闸永不收敛 → 同签名两轮 → 熔断 "
        f"fail-fast（R-1 死锁本尊）；残留 issues={after.issues}")
    writers = [st.id for st in plan.subtasks
               if agg in (list(st.scope.writable) + list(st.scope.create_files))]
    assert len(writers) == 1, f"{fx.name}: 收敛后仍有 {writers} 多个 {agg} 写者"
