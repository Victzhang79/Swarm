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

# 夹具 builders：**复用 conftest 注册的那一份**（复核 L-5）。原先无条件再 exec 一遍 →
# 同进程两个 `WorkspaceFixture` 类，而本文件同时用 `fx`（本地 exec 的）与 `make_workspace`
# （conftest 的）两条来源。今天无 `isinstance` 依赖故无害，但那是巧合不是设计。
import sys  # noqa: E402

if "stack_workspaces" in sys.modules:
    sw = sys.modules["stack_workspaces"]
else:                                     # 单独跑本文件（无 conftest 时）的兜底
    _sw = Path(__file__).resolve().parent / "stack_workspaces.py"
    _spec_sw = importlib.util.spec_from_file_location("stack_workspaces", _sw)
    sw = importlib.util.module_from_spec(_spec_sw)
    sys.modules["stack_workspaces"] = sw
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
    """R-3 回归（矩阵档）：一次 create ≥3 个该栈源码的 TRIVIAL 子任务**必须被提难度**。

    R-3 原病：`many_sources` 走 `classpath_fqn_key`（JVM 门控，对 `.ts`/`.go`/`.rs` 恒
    None）→ 非 JVM 栈判 0 个源码文件 → 难度永不提升 → worker 走 trivial 单发路径（封顶
    30 步）塞不下 → 拒答 → 全依赖链卡死（RUN19 死型）。R67-T9 这条治本**对所有非 JVM 栈
    从未生效过**。

    ★必须真调 `bump_scaffold_difficulty`（双路复核 HIGH-1/MEDIUM-1）★ 本测试原先只是把
    `is_compilable_source` 又数一遍——那是上一条测试的弱化重复，**对 R-3 零区分力**：
    两路复核各自独立把 R-3 治本撤回（`many_sources` 改回只用 `classpath_fqn_key`），
    矩阵 54 项全绿。真锁在 `test_b3::test_difficulty_bump_follows_the_table_not_java`，
    于是"矩阵档 + B-3 档双保险"是**账面双保险、实际单保险**。矩阵档的增量价值＝六型覆盖
    比 B-3 的两栈更宽。
    """
    from swarm.brain.contract_utils import bump_scaffold_difficulty
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    srcs = [s for s in fx.sources if is_compilable_source(s, fx.stack)][:3]
    assert len(srcs) >= 3, (
        f"{fx.name}: 夹具只提供 {len(srcs)} 个可编译源码，凑不出 ≥3 的判据前提"
        "（夹具得改，不是判据得改）")

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="一次建多个源码文件",
                difficulty=SubTaskDifficulty.TRIVIAL,
                scope=FileScope(create_files=list(srcs)), acceptance_criteria=["ok"]),
    ], parallel_groups=[["st-1"]], shared_contract={})

    bumped = bump_scaffold_difficulty(plan, str(fx.root))

    assert bumped >= 1, (
        f"{fx.name}: 一次 create {len(srcs)} 个源码文件仍判 trivial（bumped={bumped}）"
        "——JVM 门控回归？R-3 就是这么对非 JVM 栈全程失效的")
    assert plan.subtasks[0].difficulty != SubTaskDifficulty.TRIVIAL, (
        f"{fx.name}: 难度没真被改（bumped 计数对了但没落到子任务上——判据正确 ≠ 编排正确）")


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
        # ★断【相等】不是超集（复核 HIGH-1）★ 只断"该补的补了"会让**过度登记全程隐形**：
        # 实测把 `_SKIP_DIRS` 清空，`target/staging/Cargo.toml` 当场被登记成 workspace
        # 成员（R46-2 幽灵成员 / L8 泄漏那一族），而原先的超集断言照旧全绿。而且那张表
        # 此前**全仓无人锁**——清空它，既有的 workspace_manifest 测试也全绿。
        got = {str(m).strip("./") for m in added[fx.aggregate_manifest]}
        want = {d.strip("./") for d in fx.unregistered}
        assert got == want, (
            f"{fx.name}: 登记集不相等。多登记的={sorted(got - want)}（诱饵清单被当成员？"
            f"`_SKIP_DIRS` 失效？）少登记的={sorted(want - got)}")
        # 夹具自报的 `registered` 必须**真的已登记**（复核 L-6：原先只验它是个目录）。
        # 声明错了会让 `unregistered` 的相等断言在错的基准上成立＝假绿。
        already = {d.strip("./") for d in fx.registered}
        assert not (already & got), (
            f"{fx.name}: 夹具说 {sorted(already & got)} 已登记，reconcile 却又补了一遍"
            "——夹具声明与聚合清单实际内容分叉")
        for d in fx.unregistered:
            leaf = Path(d).name
            assert leaf in agg_text or d in agg_text, (
                f"{fx.name}: {d} 未写进 {fx.aggregate_manifest}:\n{agg_text}")
        # 诱饵绝不许出现在聚合清单里（按目录段判，不靠 basename 子串）
        for decoy in fx.decoy_manifests:
            seg = decoy.replace("\\", "/").split("/")[0]
            assert seg not in agg_text, (
                f"{fx.name}: 被排除目录 {seg}/ 的诱饵清单 {decoy} 进了聚合清单"
                f"（幽灵成员会毒死构建）:\n{agg_text}")
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
def test_demote_safety_net_tier_is_derived_from_the_path_shape(fx):
    """M-3 回归（矩阵档）：根档与模块档的兜底结论**必须能不同**，且档位由路径形状判出。

    原病：`has_manifest_reconcile` 一个布尔被消费成"该栈任何清单 demote 都有兜底" →
    go/gradle/cargo 的**模块**清单丢真实编辑连 WARNING 都没有。

    ★已改名并收窄声称（复核 note）★ 原名带 `on_a_real_tree`，但本测试**只传路径字符串、
    根本不碰磁盘**；且前两条断言是把被测函数实现原样复述（函数体就是那个三元式）。真正有
    区分力的只有**档位判定**与"无 driver 栈模块档必须无兜底"（maven/python 有 driver 的
    由 spec 字段对账锁）。行为档的强锁在
    `test_b3::test_demote_observability_is_tiered_not_one_boolean`（5 栈 × 两档探针矩阵）。
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
        if not spec.has_module_scaffold_driver:
            assert safe_mod is False, (
                f"{fx.name}: 无脚手架 driver 栈的模块清单不该有兜底网"
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


def test_every_registered_stack_has_a_workspace_fixture():
    """★真正的覆盖闸（两路复核 HIGH-2/MEDIUM-3）★ 每个已收录栈都必须有夹具。

    原先两道准入闸只管"**表**里有没有这一行"，而 ②③④⑤⑥ 全部消费者跑的是
    `WORKSPACE_BUILDERS` —— 两个集合**无对账**：加一栈只需加表项（有闸），加夹具（无闸）。
    python 当时正是此状态：`STACK_SPEC` 六栈里缺口最多的一个（`aggregate_manifest=None`、
    两档 reconcile/driver 皆无、R-3 曾对 `.py` 全程失效），却**唯一没有夹具**。
    B-0 的使命是红灯先行，少一型夹具＝**少点亮一条本该当场亮的红灯**。

    豁免要写进 `_FIXTURE_EXEMPT` 并注明理由——**别靠沉默**。
    """
    # 夹具覆盖了哪些栈，由各 builder **自报**（不手抄第二份名单）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        stacks_covered = {
            sw.build_workspace(n, Path(td) / n).stack for n in sw.WORKSPACE_BUILDERS
        }
    missing = sorted(set(STACK_SPEC) - stacks_covered - _FIXTURE_EXEMPT)
    assert not missing, (
        f"已收录栈 {missing} 没有 workspace 夹具 → ②③④⑤⑥ 全部消费者对它零覆盖，"
        f"而两道'表'准入闸照旧绿。补 builder，或写进 _FIXTURE_EXEMPT 并注明理由")


_FIXTURE_EXEMPT: frozenset[str] = frozenset()
"""显式豁免"无夹具"的栈键 —— 当前空。加进来必须在此注明理由。"""


def test_aggregate_param_set_matches_fixture_facts():
    """`AGGREGATE_WORKSPACES` 的手抄排除名单必须与夹具自报的 `aggregate_manifest` 对账。

    两者分叉会**静默漏测一整型**（聚合档不跑它），而参数计数看起来毫无异常。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for name in sw.WORKSPACE_BUILDERS:
            fx = sw.build_workspace(name, Path(td) / name)
            in_agg = name in sw.AGGREGATE_WORKSPACES
            has_agg = fx.aggregate_manifest is not None
            assert in_agg == has_agg, (
                f"{name}: 在 AGGREGATE_WORKSPACES={in_agg}，但夹具自报 "
                f"aggregate_manifest={fx.aggregate_manifest!r}——名单与事实分叉")


@pytest.mark.parametrize("fx", ALL, indirect=True)
def test_sandbox_spec_infers_a_toolchain_for_every_stack(fx, request):
    """镜像层：每型工程都必须推出对应工具链。

    ★N-1 红灯（strict xfail）★ `_infer_npm` 只读**根** package.json 的 scripts，而 npm
    workspaces 的根常常只有 `{name,private,workspaces}`（构建脚本在各子包）→ `toolchains=[]`
    → **镜像里没装 node** → 任何 npm 命令 127 → BLOCKED 无限退避。与 X-C2（Gradle 工程
    镜像没装 gradle）同型。归 B-5 SandboxDriver。
    """
    from swarm.project.sandbox_spec import find_build_files, infer_env_spec

    # ★N-1 已修（X-C2/N-1 批，`_infer_npm` 改扫全部清单）★ 原先此处对 npm_workspaces 挂
    # xfail(strict)；修好即 XPASS ⇒ 硬失败 ⇒ 逼人回来摘标记（B-0 记分板设计如此）。已摘。
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

    # ★N-2 / N-4 已治（X-H 批：`go.work` 进判据 + python 补 compileall 分支）★
    # 原先这两格挂 xfail(strict)，修好即 XPASS ⇒ 硬失败 ⇒ 逼人回来摘标记（B-0 记分板设计如此）。
    # 已摘：现在**每一型**工程都必须派生得出构建命令，没有豁免格。

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
    # 治后：**只凭磁盘**（画像=None）就必须派生得出——`go.work` 现在是 go 工程的确定性证据。
    assert _derive_full_build_command(str(fx.root), ["auth/token.go"], None) \
        == "go build ./...", "go.work 多模块仓仍派生不出 ⇒ N-2 未真治"
    # 给了画像当然也行（原判据保留：证明病根是证据源而非派生逻辑）
    assert _derive_full_build_command(
        str(fx.root), ["auth/token.go"], {"build": "go"}) == "go build ./..."
    # ★清单表仍刻意**不含** go.work★：`_BUILD_TOOL_MANIFESTS` 回答的是"跑 `go` 这个命令需要
    # 什么清单"——`go build` 要的是 go.mod（go.work 只在 workspace 模式附加）。N-2 的治法是让
    # **派生判据**认 go.work（`has("go.mod", "go.work")` + 锚到 work 根），不是往工具表里塞。
    assert "go.work" not in (_BUILD_TOOL_MANIFESTS.get("go") or ())


def test_n2_is_local_fallback_only_sandbox_branch_does_derive(make_workspace, monkeypatch):
    """★N-2 的后果**分档**（复核 HIGH-3，纠正 §7.8 原文说过头）★

    `_manifest_present` 有两条路：**沙箱优先**（`find -maxdepth 3`）与**本地兜底**
    （`os.path.isfile(root/m)`）。上面那条 xfail 跑在本地兜底上，而 worker L1 在真实沙箱
    拓扑下走前者——它翻得到 `auth/go.mod` → 派生出 `go build ./...`。**所以"go.work 多模块仓
    零构建闸"只在本地兜底成立，不是生产结论。**

    ★顺带钉住一条更值钱的★ 那条 depth-3 find 正是 **X-H1 跨栈污染**的载体："Maven 工程里
    有个 `tools/go.mod` 且只改 `.go` → 在工程根跑 `go build ./...`"。本测试把这条也实测出来，
    免得 B-5 照 §7.8 原文只修本地那半边、还拿这张网自证。
    """
    import subprocess

    from swarm.worker import l1_pipeline as l1p

    fx = make_workspace("go_work")

    class _Mgr:
        """按沙箱真实执行的那条 find 语义应答（不是想当然的 stub）。"""
        def run_command(self, sandbox, cmd, timeout=None):
            real = cmd.replace("/workspace", str(fx.root))
            out = subprocess.run(["bash", "-c", real], capture_output=True, text=True)
            return type("R", (), {"stdout": out.stdout})()

    monkeypatch.setattr(l1p, "_sandbox_ctx", lambda: (object(), _Mgr(), str(fx.root)))
    l1p._MANIFEST_PRESENT_CACHE.clear()
    try:
        assert l1p._manifest_present(("go.mod",), str(fx.root)) is True, (
            "沙箱分支的 depth-3 find 应翻得到子模块 auth/go.mod")
        l1p._MANIFEST_PRESENT_CACHE.clear()
        assert l1p._derive_full_build_command(
            str(fx.root), ["auth/token.go"], None) == "go build ./...", (
            "沙箱分支应派生得出构建命令——N-2 的'零构建闸'仅限本地兜底档")
    finally:
        l1p._MANIFEST_PRESENT_CACHE.clear()


def test_n1_npm_toolchain_inferred_from_child_package_scripts(make_workspace):
    """★N-1 已修（原定位锁转正向断言）★ `_infer_npm` 必须扫**全部** package.json。

    原病灶：只读根清单的 scripts，而 npm workspaces 的根常常只有
    `{name, private, workspaces}`、构建脚本全在子包 ⇒ 返 None ⇒ 镜像不装 node ⇒
    任何 npm 命令 127 → BLOCKED 无限退避。与 X-C2（Gradle 工程没装 gradle）同型换栈。

    下面的前提断言（清单都被发现、子包确有 build）保留——它们原是被 xfail(strict) 吞掉
    区分力的那批（复核 MEDIUM-5：把 `_NPM` 改成不匹配名会让 B-0 三个文件全绿）。

    `xfail(strict=True)` 会让同一测试内**所有先行断言失去区分力**——npm 那格被吞掉的包括
    `assert bf`（`find_build_files` 到底认不认 `package.json`）与全部夹具形状前提。实测：
    把 `sandbox_spec._NPM` 改成不匹配名（npm 构建文件识别整条失效），B-0 三个文件**全绿**。
    N-2 有定位锁、N-2b/N-3 的前提句碰巧被同文件兄弟测试覆盖，**N-1 两者皆无**。
    """
    from swarm.project.sandbox_spec import _infer_npm, find_build_files

    fx = make_workspace("npm_workspaces")
    pkgs = find_build_files(fx.root).get("npm") or []
    # 前提①：根与子包的 package.json 都被发现（这才是 N-1 说"只读根"的对照面）
    assert "package.json" in pkgs, f"根 package.json 未被发现：{pkgs}"
    assert any("/" in p for p in pkgs), f"子包 package.json 未被发现：{pkgs}"
    # 前提②：子包确有 build script（形状不对就测成另一条命题了——本批教训 2）
    import json
    child = json.loads((fx.root / "packages/web/package.json").read_text(encoding="utf-8"))
    assert child.get("scripts", {}).get("build"), "夹具子包缺 build script，N-1 前提不成立"
    # 治后：子包有 build ⇒ 必须推出 node 工具链（否则镜像不装 node → 127 死循环）
    tc = _infer_npm(fx.root, pkgs)
    assert tc is not None, "子包有 build script 却推不出 node 工具链 ⇒ 镜像不装 node ⇒ 127"
    assert tc.name == "node" and tc.build_tool == "npm"
    # workspaces 的依赖提升到根 ⇒ warmup 必须在**根**跑 npm ci
    assert tc.dep_source == "package.json", (
        f"workspaces 根声明了 workspaces，dep_source 应指根清单，实得 {tc.dep_source!r}")


def test_n1_static_resources_still_get_no_node(tmp_path):
    """★回归臂：别把"不装"整类放宽掉★ 无任何构建脚本的 package.json（Maven 单体里的
    Thymeleaf/admin 静态资源）仍不装 node —— 那是 st-10 的治法（装了会误派 npm 构建 →
    BLOCKED 空转）。N-1 的修法只把判据从"根有没有"放宽到"任一有没有"，不碰这条结论。"""
    from swarm.project.sandbox_spec import _infer_npm

    (tmp_path / "src" / "main" / "resources" / "static").mkdir(parents=True)
    (tmp_path / "package.json").write_text('{"name": "static", "private": true}')
    (tmp_path / "src" / "main" / "resources" / "static" / "package.json").write_text(
        '{"name": "vendor-assets", "dependencies": {"jquery": "3.7.1"}}')
    assert _infer_npm(tmp_path, ["package.json",
                                 "src/main/resources/static/package.json"]) is None


def test_n1_single_frontend_in_subdir_points_warmup_at_it(tmp_path):
    """根无 workspaces 且根无脚本、只有子目录前端有脚本（`ui/package.json` 常见形态）→
    `dep_source` 必须指那个子目录，否则 npm warmup 会 cd 到工程根跑 `npm ci` 装错地方。"""
    from swarm.project.sandbox_spec import _infer_npm

    (tmp_path / "ui").mkdir()
    (tmp_path / "package.json").write_text('{"name": "root", "private": true}')
    (tmp_path / "ui" / "package.json").write_text(
        '{"name": "ui", "scripts": {"build": "vite build"}}')
    tc = _infer_npm(tmp_path, ["package.json", "ui/package.json"])
    assert tc is not None and tc.dep_source == "ui/package.json"


# ══════════════════════════════════════════════
# ⑥ R-1 收敛闭环：判死的必须能被收敛（真实磁盘树档）
# ══════════════════════════════════════════════

@pytest.mark.parametrize("fx", AGG, indirect=True)
def test_r1_condemned_aggregate_writers_converge_on_a_real_tree(fx):
    """★R-1 本尊的矩阵档★ 两个子任务都写根聚合清单 → 收敛器必须当场收敛成单写者。

    R-1 原病：判死的名单（`plan_validator` 硬闸）⊅ 收敛的名单（规则1/规则4）→
    `go.work`/`settings.gradle`/`Cargo.toml` **判死却无人收敛** → 规划期硬闸永不收敛 →
    同签名连续两轮 → **熔断 fail-fast** → 那三栈的多模块工程 100% 死在规划期。

    ★增量价值是**六型覆盖**，不是"真实磁盘树"（复核 MEDIUM-2 纠正）★
    本测试原 docstring 声称"跑在真实磁盘树上，规则4 owner 探测与聚合-vs-新建分流都读真磁盘"。
    实测证伪：对全部 AGG 夹具，`(before.valid, after.valid, writers)` 在**真实树**与**空目录**
    上逐字相同（全部 `(False, True, ['st-1'])`）——观测面对磁盘完全不敏感。R-1 收敛本身是真锁
    （突变 M2/M11 验红），但"真实树"这个增量为零，别再拿它当双保险的第二重。
    真正的差别只有一条：这里**逐型**跑（六型），`test_b3` 那边是两栈合成 plan。
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
