"""B-3：STACK_SPEC 单一事实源 —— R-1 确定性死锁的回归锁 + 禁裸表。

## R-1 是什么

规划期曾有两份手抄名单，一处**判死**、一处**收敛**，而它们不是同一张表：

    plan_validator._ROOT_AGGREGATOR_MANIFESTS  5 条（**漏 package.json**）
    contract_utils 规则1/规则4 的 owner 收敛     **只认 pom.xml**

  · go.work / settings.gradle / Cargo.toml：判死却无人收敛 → 规划期硬闸永不收敛
    → 同签名连续两轮 → **熔断 fail-fast** → 那三个栈的多模块工程 100% 死在规划期，
    而日志里只有"同签名不收敛"，看不出根因是"没人会收敛 go.work"；
  · package.json：漏判 → npm workspaces 双写者放行 → 聚合结构重写**非加性**
    → 后写者覆盖前写者的注册 → 丢 workspace。

两份名单一个多一个少，互为对方的反证。

## 本文件断言什么

**不断言实现细节**（纪律 6 禁 getsource/正则扫源码），断言的是
①两侧派生自同一张表这一**接线事实**；②**行为**——判死的东西必须能被收敛。
"""

from __future__ import annotations

import pytest

from swarm.brain import contract_utils as cu
from swarm.brain.plan_validator import validate_plan_structure
from swarm.stacks import (
    STACK_SPEC,
    aggregate_manifests_of_stack,
    is_structural_build_manifest,
    module_manifests_of_stack,
    root_aggregate_manifests,
    spec_for_stack,
    structural_manifests,
    unregistered_aggregate_stacks,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


def _plan(*subtasks):
    return TaskPlan(subtasks=list(subtasks), parallel_groups=[], shared_contract={})


def _st(sid, *, create=(), writable=(), difficulty=SubTaskDifficulty.MEDIUM, deps=()):
    return SubTask(id=sid, description=f"task {sid}", difficulty=difficulty,
                   scope=FileScope(create_files=list(create), writable=list(writable)),
                   depends_on=list(deps), acceptance_criteria=["ok"])


# ══════════════════════════════════════════════
# ① 接线事实：判死的名单 == 派生视图（不许再手抄一份）
# ══════════════════════════════════════════════

def test_validator_hard_gate_reads_the_table_live_not_a_frozen_copy():
    """硬失败闸必须**在调用时**读表，不许 import 期冻一份副本。

    ★复核 F-7 整改★ 先前是 `_ROOT_AGGREGATOR_MANIFESTS = root_aggregate_manifests()`
    模块常量 + 一条相等断言。相等断言证不了"闸真的读表"（谁再抄一份、两边同时改就过），
    冻结本身还让"新增一栈只加一条表项"的承诺**在闸侧失效**——运行期新栈进不了冻结集合。
    本测试直接**在运行期**往表里塞一栈，看闸是否当场认它：这才是接线事实（纪律 6 允许）。
    """
    from swarm.stacks import spec as spec_mod

    probe = spec_mod.StackSpec(
        key="probeish", lang="probe",
        root_manifests=("probe.workspace",),
        module_manifest="probe.module",
        aggregate_manifest="probe.workspace", aggregate_field="[members]",
        source_exts=(".probe",),
    )
    STACK_SPEC["probeish"] = probe
    try:
        plan = _plan(
            _st("st-1", writable=["probe.workspace"]),
            _st("st-2", writable=["probe.workspace"]),
        )
        issues = " ".join(validate_plan_structure(plan).issues)
        assert "根聚合清单 probe.workspace" in issues, (
            "闸没有当场认新表项 → 它读的是冻结副本，不是表。issues=" + issues)
    finally:
        STACK_SPEC.pop("probeish", None)


@pytest.mark.parametrize("manifest", sorted(root_aggregate_manifests()))
def test_every_condemnable_manifest_is_also_convergeable(manifest):
    """★R-1 本尊的行为锁★：凡是硬失败闸会判死的根聚合清单，收敛器都必须救得回来。

    这条一旦红，意味着又出现了"判死却无人能救"的组合 —— 那不是"多栈支持弱"，
    是那个栈的多模块工程**必定**死在规划期熔断。

    参数化直接跑遍派生视图：**新增一栈自动进本测试**，不需要谁记得来加一行。
    """
    plan = _plan(
        _st("st-1", create=[f"mod-a/{manifest}"], writable=[manifest]),
        _st("st-2", create=[f"mod-b/{manifest}"], writable=[manifest]),
    )
    assert not validate_plan_structure(plan).valid, "前提：双写根聚合清单必须先被判死"

    orig = cu._exists_in_repo
    cu._exists_in_repo = lambda pp, rel, cache, base_ref=None: rel == manifest
    try:
        cu.resolve_plan_conflicts(plan, project_path="/fixture/repo")
    finally:
        cu._exists_in_repo = orig

    result = validate_plan_structure(plan)
    assert result.valid, (
        f"{manifest}：硬闸判死但确定性 pass 收敛不了 → 规划期永不收敛 → 熔断 fail-fast。"
        f"issues={result.issues}")


@pytest.mark.parametrize("agg,mod,field_hint", [
    ("go.work", "go.mod", "use"),
    # ★复核 M-1/F-1 本尊★ spec 声明 `.kts` 与 canonical 同档消费，而规则4 曾只比单数字段
    # → KTS 工程 owner 拿不到任何登记验收条目（实测），spec 宣称的"reconcile + 规则4 双保险"
    # 对 KTS 只剩一道。这一行就是那条病的行为锁。
    ("settings.gradle.kts", "build.gradle.kts", "include"),
    ("settings.gradle", "build.gradle", "include"),
])
def test_owner_still_registers_every_new_module(agg, mod, field_hint):
    """收敛不等于丢登记：唯一 owner 必须显式承担【全部】新模块的注册（含被 demote 者的）。

    demote 只是收回写权，登记责任要落到 owner 的验收条目上——否则"收敛成功"
    的代价是静默少注册一个模块，比不收敛更难查。
    """
    plan = _plan(
        _st("st-1", create=[f"mod-a/{mod}"], writable=[agg]),
        _st("st-2", create=[f"mod-b/{mod}"], writable=[agg]),
    )
    orig = cu._exists_in_repo
    cu._exists_in_repo = lambda pp, rel, cache, base_ref=None, _a=agg: rel == _a
    try:
        cu.resolve_plan_conflicts(plan, project_path="/fixture/repo")
    finally:
        cu._exists_in_repo = orig

    owners = [s for s in plan.subtasks
              if agg in (list(s.scope.writable or []) + list(s.scope.create_files or []))]
    assert len(owners) == 1, [s.id for s in owners]
    note = " ".join(owners[0].acceptance_criteria or [])
    assert "mod-a" in note and "mod-b" in note, note
    assert agg in note and field_hint in note, "验收条目须点名清单与登记字段（栈相关文案）"


def test_alias_manifests_are_consumed_at_the_same_tier_as_canonical():
    """★spec 声明"别名与 canonical 同档消费"，本条锁的是**真的同档**★

    复核实测过的两处落空（都因为只读单数字段）：
      · 难度 bump：写根 `settings.gradle.kts` 的 TRIVIAL 脚手架 bumped=0 保持 trivial
        → RUN19 那条"读大根清单塞不下 → 单发拒答 → 全依赖链卡死"在 KTS 上原样活着；
      · 规则4 登记：KTS 模块清单不进 new_modules → owner 零登记条目。
    参数化遍历**表里声明的全部别名**：将来给任何栈加别名都自动进本测试。
    """
    aliases = [(s.key, a) for s in STACK_SPEC.values()
               for a in s.aggregate_extra_manifests]
    assert aliases, "表里一个别名都没有？那 aggregate_extra_manifests 这个字段就是死的"
    for stack, alias in aliases:
        # ① 判死档：别名必须与 canonical 一样进根聚合集
        assert alias in root_aggregate_manifests(), f"{stack}:{alias} 漏判死档"
        # ② 收敛档
        assert is_structural_build_manifest(alias), f"{stack}:{alias} 漏收敛档"
        # ③ 难度档（行为，非集合）
        st = _st("st-1", writable=[alias], difficulty=SubTaskDifficulty.TRIVIAL)
        plan = _plan(st)
        cu.bump_scaffold_difficulty(plan, project_path=None)
        assert st.difficulty != SubTaskDifficulty.TRIVIAL, (
            f"{stack}:{alias} 根清单脚手架未提难度 → R-2 在该别名上未生效")
    for stack, alias in [(s.key, a) for s in STACK_SPEC.values()
                         for a in s.module_extra_manifests]:
        assert is_structural_build_manifest(alias), f"{stack}:{alias} 模块别名漏收敛档"
        assert alias in module_manifests_of_stack(stack)


def test_plan_paths_match_case_insensitively():
    """★复核 F-2★ 判 **LLM 写的 plan 路径**必须大小写不敏感——"LLM 写的路径大小写不可信"
    是本仓既有认知（`_MANIFEST_TO_STACK_LC` 同款）。

    实测过的洞：LLM 写小写 `cargo.toml` 的根双写者，既不吃判死闸、也不吃 demote 收敛
    → 两个写者各自整段重写 `[workspace] members` → 非加性覆盖，后写者抹掉前写者的注册。
    ★磁盘探测侧刻意保持规范大小写★（Linux `os.path.exists` 大小写敏感），两处不可互换。
    """
    from swarm.stacks import is_root_aggregate_manifest

    assert is_root_aggregate_manifest("cargo.toml")
    assert is_root_aggregate_manifest("Cargo.toml")
    assert is_structural_build_manifest("crates/a/cargo.toml")
    plan = _plan(_st("st-1", writable=["cargo.toml"]), _st("st-2", writable=["cargo.toml"]))
    assert "根聚合清单" in " ".join(validate_plan_structure(plan).issues), (
        "小写 cargo.toml 逃过判死闸 → 非加性覆盖敞开")
    # 表里 root_manifests 仍是规范大小写（磁盘探测靠它）
    assert "Cargo.toml" in spec_for_stack("cargo").root_manifests


# ══════════════════════════════════════════════
# ② 分档：两个集合语义不同，不许互相顶替
# ══════════════════════════════════════════════

def test_structural_and_aggregate_sets_are_not_interchangeable():
    """`structural_manifests`（多写者收敛，含模块级）⊋ `root_aggregate_manifests`（根级判死）。

    合并成一个集合就会出事：拿 structural 去判死会把 `mod-a/pom.xml` 也判成根聚合；
    拿 aggregate 去做收敛会漏掉模块清单的双写者。
    """
    assert root_aggregate_manifests() < structural_manifests()
    assert "build.gradle" in structural_manifests()
    assert "build.gradle" not in root_aggregate_manifests(), "模块清单不是根聚合登记点"


def test_python_is_deliberately_out_of_the_demote_set():
    """★后果分档的诚实边界★：python 不进 structural 集合是【刻意】的，不是遗漏。

    demote 会收回非 owner 的写权，只有在登记有确定性补回路径时才安全：
    maven/gradle/cargo/go 有 `_reconcile_*` 据磁盘补齐 + 规则4 owner 登记＝双保险；
    python 的 workspace 机制生态碎片化（poetry/uv/hatch 互不兼容）→ 本表未收录聚合
    → 既无 reconcile 也无规则4 登记 → demote 必丢贡献。故维持既有"串行化保留写权"。
    """
    assert "python" in unregistered_aggregate_stacks()
    assert spec_for_stack("python").aggregate_manifest is None
    assert "pyproject.toml" not in structural_manifests()
    assert not is_structural_build_manifest("pyproject.toml")


def test_absence_is_machine_readable():
    """缺席必须机读可辨（纪律：`return []` 与"真没有"不可分时，那一层能死很久没人知道）。

    `unregistered_aggregate_stacks()` 就是那个机读键，**且有消费者**——上面两条测试
    与 B-7 的覆盖面登记都读它。新账没有消费者＝没造。
    """
    unreg = unregistered_aggregate_stacks()
    assert isinstance(unreg, tuple)
    for key in unreg:
        assert spec_for_stack(key).aggregate_manifest is None
    for key, spec in STACK_SPEC.items():
        if spec.aggregate_manifest is None:
            assert key in unreg, f"{key} 未收录聚合机制却不在机读账里"


def _capture_demote(contested, *, exists):
    """跑一次"双写者争抢 `contested`"的收敛，回收 (WARNING 文案列表, degrade 键列表)。

    刻意**只争一个文件**：否则聚合档探针会顺带造出一个模块档 demote，两档信号混在一起，
    断言就得靠文案过滤才站得住（那样"档分对了"就没被真正证明）。
    """
    import logging

    logging.disable(logging.NOTSET)          # 全套件乱序鲁棒（本仓既有范式）
    lg = logging.getLogger(cu.__name__)
    seen: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    degraded: list[str] = []
    from swarm.infra import degrade as dg
    orig_rec, orig_exists = dg.record_degrade, cu._exists_in_repo
    dg.record_degrade = lambda k, *a, **kw: degraded.append(k)
    h = _H(level=logging.WARNING)
    lg.addHandler(h)
    old_level = lg.level
    lg.setLevel(logging.WARNING)
    try:
        plan = _plan(_st("st-1", writable=[contested]), _st("st-2", writable=[contested]))
        cu._exists_in_repo = lambda pp, rel, cache, base_ref=None: rel in exists
        cu.resolve_plan_conflicts(plan, project_path="/fixture/repo")
        return [m for m in seen if "无兜底网" in m], degraded
    finally:
        dg.record_degrade, cu._exists_in_repo = orig_rec, orig_exists
        lg.removeHandler(h); lg.setLevel(old_level)


@pytest.mark.parametrize("agg,mod,warn_agg,warn_mod", [
    # 栈, 根聚合档, 模块档 —— 两档**分别**判，别用一个布尔管两件事
    ("pom.xml", "mod-a/pom.xml", False, False),        # maven：两档都有网
    ("package.json", "packages/a/package.json", True, True),   # npm：两档都无网
    # ★复核 M-3 本尊★ gradle/go/cargo 有【聚合】reconcile 但**没有模块清单的网**：
    # 早先版本拿聚合档的事实当"该栈任何清单都有网"，模块清单 demote 丢真实编辑却零告警。
    ("settings.gradle", "mod-a/build.gradle", False, True),
    ("go.work", "internal/a/go.mod", False, True),
    ("Cargo.toml", "crates/a/Cargo.toml", False, True),
])
def test_demote_observability_is_tiered_not_one_boolean(agg, mod, warn_agg, warn_mod):
    """★demote 收回写权，**该档**无兜底网就必须留痕（纪律 3 + "缺席须机读可辨"）★

    两档事实不同、后果不同，故必须分别判：
      · 根聚合档 → `has_aggregate_reconcile`（`_reconcile_*` 据磁盘补回根注册）；
      · 模块清单档 → `has_module_scaffold_driver`（owner 按契约一次建全，非 owner 本无
        合法贡献 = #11a doctrine）。只有 maven 有后者。
    把前者当"该栈任何清单都有网"用，gradle/cargo/go 的模块清单被 demote 时丢的是**真实
    编辑**（该子任务想加的依赖/插件），却连一句 WARNING 都没有——那正是"降级无痕"。

    机读账走 `record_degrade`（**有真实消费者**：`/api/metrics` 降级面），不是新造没人读的键。
    """
    # ① 根聚合档：根清单已在 repo，两个子任务都想写它 → 非首写者 demote 根清单
    warns, degraded = _capture_demote(agg, exists={agg})
    assert bool(warns) is warn_agg, f"{agg} 聚合档留痕期望 {warn_agg}，实得 {warns}"
    if warn_agg:
        assert all("档=aggregate" in w for w in warns), warns
        assert any(k.endswith(":aggregate") for k in degraded), degraded

    # ② 模块清单档：模块清单已在 repo（既有模块），两个子任务都想改它 → 非首写者 demote 模块清单
    warns2, degraded2 = _capture_demote(mod, exists={mod})
    assert bool(warns2) is warn_mod, f"{mod} 模块档留痕期望 {warn_mod}，实得 {warns2}"
    if warn_mod:
        assert all("档=module" in w for w in warns2), warns2
        assert any(k.endswith(":module") for k in degraded2), degraded2


def test_reconcile_facts_match_reality():
    """`has_aggregate_reconcile` 是**事实**，不是心愿——必须与 workspace_manifest 真有的函数对账。

    这条防的是：将来谁加了 `_reconcile_npm` 却忘了把 spec 里的 False 翻成 True
    （→ 白刷告警），或反过来删了某个 reconcile 而 spec 还写着 True（→ 静默丢贡献回来）。
    用**模块自身的函数集**做单一事实源（纪律 6 允许断"接线事实"）。
    """
    from swarm.worker import workspace_manifest as wm

    present = {n for n in dir(wm) if n.startswith("_reconcile_")}
    for key, spec in STACK_SPEC.items():
        # go 的 reconcile 落在 `_reconcile_go_work`，故按前缀匹配栈键
        has = any(n.startswith(f"_reconcile_{key}") for n in present)
        assert spec.has_aggregate_reconcile == has, (
            f"{key}: spec 写 {spec.has_aggregate_reconcile}，"
            f"workspace_manifest 实际 {has}（present={sorted(present)}）")


def test_scaffold_driver_facts_match_reality():
    """`has_module_scaffold_driver` 同样是**事实**——与 contract_utils 的脚手架栈集对账。

    只有该集合里的栈才有"owner 按契约一次建全模块清单"这个前提；没有它，模块清单
    demote 掉的是真实编辑。将来谁给 gradle 写了 aggregator/模块脚手架 driver（B-5/B-6），
    这条会红，提醒他同步把 spec 的 False 翻成 True（否则白刷告警）。
    """
    for key, spec in STACK_SPEC.items():
        assert spec.has_module_scaffold_driver is (key in cu._AGGREGATOR_SCAFFOLD_STACKS), (
            f"{key}: spec 写 {spec.has_module_scaffold_driver}，"
            f"_AGGREGATOR_SCAFFOLD_STACKS={sorted(cu._AGGREGATOR_SCAFFOLD_STACKS)}")


# ══════════════════════════════════════════════
# ③ 禁裸表：新增一栈只需改 STACK_SPEC
# ══════════════════════════════════════════════

def test_new_stack_needs_no_caller_change():
    """★通用性锁★（照 dep_legality 的玩具 driver 先例）：注册一个玩具栈即可让
    判死闸与收敛器同时管它——**一行调用方代码都不用改**。

    锁死"别为某一栈写死"。这条红了就说明又有人在调用方那边写了 `if stack == ...`。
    """
    from swarm.stacks import spec as spec_mod

    toy = spec_mod.StackSpec(
        key="toyish", lang="toy",
        root_manifests=("toy.workspace",),
        module_manifest="toy.module",
        aggregate_manifest="toy.workspace", aggregate_field="[members]",
        source_exts=(".toy",),
    )
    STACK_SPEC["toyish"] = toy
    try:
        assert "toy.workspace" in root_aggregate_manifests()
        assert is_structural_build_manifest("pkg/toy.module")

        plan = _plan(
            _st("st-1", create=["mod-a/toy.module"], writable=["toy.workspace"]),
            _st("st-2", create=["mod-b/toy.module"], writable=["toy.workspace"]),
        )
        # ★复核 F-7 整改★ 旧断言只写 `not valid` 并注释"新栈自动获得根聚合硬闸"——
        # 那是**假过**：双无依赖写者本就被通用"并行必冲突"分支判死，根聚合闸有没有认它
        # 完全没被证明。改断具体文案（根聚合分支带 `continue`，两条分支互斥）。
        _issues = " ".join(validate_plan_structure(plan).issues)
        assert "根聚合清单 toy.workspace" in _issues, (
            "新栈没自动获得【根聚合】硬闸（落到通用写冲突分支了）：" + _issues)

        orig = cu._exists_in_repo
        cu._exists_in_repo = lambda pp, rel, cache, base_ref=None: rel == "toy.workspace"
        try:
            cu.resolve_plan_conflicts(plan, project_path="/fixture/repo")
        finally:
            cu._exists_in_repo = orig
        assert validate_plan_structure(plan).valid, "新栈也必须自动获得收敛，绝不留死锁"
    finally:
        STACK_SPEC.pop("toyish", None)


@pytest.mark.parametrize("path,stack,expect,why", [
    ("pkg/a.go", "go", True, "普通源码"),
    ("src/a.ts", "npm", True, "普通源码"),
    ("pkg/a_test.go", "go", True, "★测试文件刻意计入★：人手写、要过编译，3 个就是多步工作"),
    ("pkg/a.spec.ts", "npm", True, "同上"),
    ("pkg/types.d.ts", "npm", False, "纯类型声明，无编译产物"),
    ("vendor/x/y.go", "go", False, "vendored，从不由人手写"),
    ("node_modules/x/i.js", "npm", False, "同上"),
    ("dist/bundle.js", "npm", False, "构建产物"),
    ("target/gen/A.rs", "cargo", False, "构建产物"),
    ("pkg/a.md", "go", False, "文档不参与编译"),
    ("pkg/a.go", None, False, "栈未知 → fail-closed"),
    ("pkg/a.go", "toy-unknown", False, "未收录栈 → fail-closed，绝不回退某个默认栈"),
])
def test_compilable_source_boundaries(path, stack, expect, why):
    """`is_compilable_source` 的边界——误计的方向是过度提难度＝白占小模型算力
    （R62-Task6 点过的路由异味），故排除项要真、但不能把人手写的测试文件也排掉。"""
    from swarm.stacks import is_compilable_source
    assert is_compilable_source(path, stack) is expect, why


def test_rule4_registers_by_plan_evidence_not_by_fabrication_gate():
    """★复核 F-4★ 混栈（plan 有 .java + package.json、根上还没 pom）时规则4 必须照样登记。

    `_detect_build_stack` 的 `_has_jvm_src` 护栏刻意把这种歧义混栈判成 unknown→保守回退
    Maven（防后端模块静默丢 pom = round62 家族级回归）。但规则4 复用那个返回值就成了：
    unknown → 聚合清单 None → **整条规则静默跳过**，npm workspaces 登记一个字都不写、
    且零日志；而 demote 留痕还照旧宣称"登记仅靠规则4 owner 一道网"，实际是**零道网**。
    登记是**加性**动作（不像伪造脚手架有风险），按 plan 里实际出现的清单证据走才对。
    """
    plan = _plan(
        _st("st-1", create=["packages/a/package.json", "packages/a/src/A.java"],
            writable=["package.json"]),
        _st("st-2", create=["packages/b/package.json"], writable=["package.json"]),
    )
    orig = cu._exists_in_repo
    cu._exists_in_repo = lambda pp, rel, cache, base_ref=None: rel == "package.json"
    try:
        cu.resolve_plan_conflicts(plan, project_path="/fixture/repo")
    finally:
        cu._exists_in_repo = orig

    owners = [s for s in plan.subtasks if "package.json" in (
        list(s.scope.writable or []) + list(s.scope.create_files or []))]
    assert len(owners) == 1, [s.id for s in owners]
    note = " ".join(owners[0].acceptance_criteria or [])
    assert "workspaces" in note and "packages/a" in note and "packages/b" in note, (
        "混栈时 npm 登记意图整体缺席（规则4 被 unknown 静默跳过）：" + note)
    # 伪造闸自身保持保守（该护栏是防 round62 的，绝不因本改动松动）
    assert cu._detect_build_stack(plan, None) == "unknown"


def test_detection_table_drift_is_accounted():
    """`_detect_build_stack` 的 `_MANIFEST_TO_STACK` 与 `spec.root_manifests` 的差集必须**有账**。

    ★复核 F-6★ 两表已漂移：python 的 `requirements.txt`/`setup.py` 在 spec 里、检测表里没有
    → 只写 requirements.txt 的 python plan 判 unknown → R-3（多源码提难度）对 .py 无声失效。
    **本批刻意不动检测表**：把它们加进去会让 `_should_fabricate_maven_scaffold` 对纯 python
    仓从 True 翻成 False（后果档不同——那是"要不要伪造 pom"，不是"这是哪个栈"），属 B-6 范围。
    本测试把这份差集**钉成显式清单**：将来任何一侧变动都会红，逼人当场决定而不是继续漂。
    """
    spec_names = {n for s in STACK_SPEC.values() for n in s.root_manifests}
    missing = {n for n in spec_names if n.lower() not in cu._MANIFEST_TO_STACK_LC}
    assert missing == {"requirements.txt", "setup.py"}, (
        f"两表差集变了（实得 {sorted(missing)}）。加进检测表会改 pom 伪造闸的行为档，"
        f"请按 B-6 的口径处置后再更新本断言，别默默改表。")


@pytest.mark.parametrize("manifest", sorted(structural_manifests()))
def test_rule0_never_relocates_any_build_manifest(tmp_path, manifest):
    """★复核 F-5★ 规则0 的"构建清单一律不按 basename 重定位"必须覆盖**全部**结构清单。

    规则0 处理"writable 指向 base 树里没有的路径"（LLM 幻觉路径）：basename 在 base 树
    唯一命中 → 确定性重定位到真身；**但构建清单例外**——新模块清单被误标 writable 是 LLM
    常见形态，按 basename 撞上根清单就是本块注释②自述的"击穿 D1 单写者 + 脚手架蒸发"。
    旧码是手抄名单、**漏 go.work**（`Cargo.toml` 靠 `.lower()` 侥幸兜住），派生后才真覆盖全。

    参数化跑遍派生集：新增一栈/别名自动进本测试。
    """
    import subprocess

    proj = tmp_path / "proj"
    (proj / "existing").mkdir(parents=True)
    (proj / "existing" / manifest).write_text("x")     # base 树里的同名清单（唯一命中）
    (proj / "keep.txt").write_text("k")
    for a in ("init", "-q"), ("add", "-A"):
        subprocess.run(["git", *a], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], cwd=proj, check=True, capture_output=True)

    # st-1 把【新模块】清单误标进 writable（base 树里不存在这条路径）
    st = _st("st-1", writable=[f"newmod/{manifest}"])
    cu.normalize_plan_scopes(_plan(st), project_path=str(proj))

    assert f"newmod/{manifest}" in (st.scope.create_files or []), (
        f"{manifest} 未被判为构建清单 → 走了重定位分支，击穿 D1 单写者 + 脚手架蒸发。"
        f"create={st.scope.create_files} writable={st.scope.writable}")
    assert f"existing/{manifest}" not in (st.scope.writable or []), (
        f"{manifest} 被按 basename 重定位到 base 树同名命中处：{st.scope.writable}")


def test_difficulty_bump_follows_the_table_not_java():
    """R-2/R-3：根清单脚手架提难度、多源码文件提难度，两条都不得是 Java 专属。

    RUN19 死因是"trivial 单发路径（封顶 30 步）塞不下读改大根清单"——这与语言无关。
    """
    for agg, mod, ext in (("package.json", "packages/a/package.json", ".ts"),
                          ("go.work", "internal/a/go.mod", ".go"),
                          ("pom.xml", "mod-a/pom.xml", ".java")):
        scaffold = _st("st-1", create=[mod], writable=[agg],
                       difficulty=SubTaskDifficulty.TRIVIAL)
        # JVM 侧刻意用带物理模块根的路径：`classpath_fqn_key` 要求能定位模块根，
        # 这是 JVM 既有的**更严**口径（本批不动它），非 JVM 侧按源码后缀判。
        srcs = ([f"mod-a/src/main/java/com/x/{n}{ext}" for n in "ABC"] if ext == ".java"
                else [f"pkg/{n}{ext}" for n in "abc"])
        multi = _st("st-2", difficulty=SubTaskDifficulty.TRIVIAL, create=srcs)
        plan = _plan(scaffold, multi)
        cu.bump_scaffold_difficulty(plan)
        assert scaffold.difficulty != SubTaskDifficulty.TRIVIAL, f"{agg} 根清单脚手架未提难度"
        assert multi.difficulty != SubTaskDifficulty.TRIVIAL, f"{ext} 多源码文件未提难度"
