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

import logging

import pytest

from swarm.brain import contract_utils as cu
from swarm.brain.plan_validator import validate_plan_structure
from swarm.stacks import (
    STACK_SPEC,
    aggregate_manifests_of_stack,
    build_manifest_basenames,
    is_structural_build_manifest,
    module_manifests_of_stack,
    root_aggregate_manifests,
    root_manifests_by_stack,
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
    # ★复核 M-3 本尊★ gradle 有【聚合】reconcile 但**没有模块清单的网**：
    # 早先版本拿聚合档的事实当"该栈任何清单都有网"，模块清单 demote 丢真实编辑却零告警。
    # P-H4c 起 gradle 模块档也有 #31-P2f 确定性 driver → (False, False)（M-3 原格已翻）。
    ("settings.gradle", "mod-a/build.gradle", False, False),
    # npm/go/python/cargo 模块档已有 #31-P2 确定性 driver（owner 按契约一次建全+backfill，
    # P-H4a/b 复核补翻 spec）→ 模块清单 demote 安全不刷告警；npm 聚合档仍无 reconcile。
    ("package.json", "packages/a/package.json", True, False),
    ("go.work", "internal/a/go.mod", False, False),
    ("Cargo.toml", "crates/a/Cargo.toml", False, False),
    # ★python 刻意不在本矩阵★（hunter R2 L-5：空转行零区分力——python 不在
    # structural_manifests，writable 双写走串行化不 demote，两探针恒零告警）。
    # python 模块档 has_module_scaffold_driver=True 的真实消费者=【新建撞车 demote
    # 路径】，判别锁在 test_python_module_manifest_create_collision_demote_is_silent。
])
def test_demote_observability_is_tiered_not_one_boolean(agg, mod, warn_agg, warn_mod):
    """★demote 收回写权，**该档**无兜底网就必须留痕（纪律 3 + "缺席须机读可辨"）★

    两档事实不同、后果不同，故必须分别判：
      · 根聚合档 → `has_aggregate_reconcile`（`_reconcile_*` 据磁盘补回根注册）；
      · 模块清单档 → `has_module_scaffold_driver`（owner 按契约一次建全，非 owner 本无
        合法贡献 = #11a doctrine）。有确定性 driver 的栈（maven 聚合 driver +
        #31-P2 npm/go/python/cargo 模块 driver）才有后者，对账锁防"driver 落地忘翻字段"。
    把前者当"该栈任何清单都有网"用，gradle 的模块清单被 demote 时丢的是**真实
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


def test_python_module_manifest_create_collision_demote_is_silent():
    """★P-H4a 复核 hunter#1 的真实消费者★ python 模块 pyproject.toml【新建撞车】的 demote
    （非首写者收写权）在 driver 落地后是安全的（owner 按契约一次建全+backfill，#11a
    doctrine）→ 不得再刷「无兜底网」告警/记 degrade 账。

    判别力：①demote 本身【必须发生】（st-2 失去新建权）——否则「不告警」只是「根本没
    demote」的空转通过；②把 spec 的 has_module_scaffold_driver 翻回 False 本测试必须红
    （突变判据）；③python 不在 structural_manifests（根档无网、basename 无法分根/模块），
    所以本路径走的是「新建撞车 demote」而非 `_is_pom_file` 整段重写闸。"""
    contested = "pkg-a/pyproject.toml"
    import logging
    logging.disable(logging.NOTSET)
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
        cu._exists_in_repo = lambda pp, rel, cache, base_ref=None: False  # 纯新建撞车
        # st-2 必须再带一个代码文件：纯清单副本会先被 dedupe_module_scaffolds 合并删除
        # （那是 R-2 修法的行为），到不了规则1 demote——本测试锁的是 demote 的留痕档。
        plan = _plan(_st("st-1", create=[contested]),
                     _st("st-2", create=[contested, "pkg-a/main.py"]))
        cu.resolve_plan_conflicts(plan, project_path="/fixture/repo")
    finally:
        dg.record_degrade, cu._exists_in_repo = orig_rec, orig_exists
        lg.removeHandler(h); lg.setLevel(old_level)
    st2 = next(s for s in plan.subtasks if s.id == "st-2")
    # ① demote 确实发生（非首写者失去新建权）——这条红了说明测的是空转
    assert contested not in list(getattr(st2.scope, "create_files", []) or [])
    # ② driver 就是网 → 不刷「无兜底网」、不记降级账
    assert not [w for w in seen if "无兜底网" in w], seen
    assert not [k for k in degraded if ":python:" in k], degraded


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

    只有 `_MODULE_SCAFFOLD_DRIVER_STACKS`（maven 聚合 driver ∪ `_P2_SCAFFOLD_DRIVERS`）
    里的栈才有"owner 按契约一次建全模块清单"这个前提；没有它，模块清单 demote 掉的是
    真实编辑。将来谁给 gradle 写了模块脚手架 driver（P-H4 剩余），这条会红，
    提醒他同步把 spec 的 False 翻成 True（否则白刷告警）。
    """
    for key, spec in STACK_SPEC.items():
        assert spec.has_module_scaffold_driver is (key in cu._MODULE_SCAFFOLD_DRIVER_STACKS), (
            f"{key}: spec 写 {spec.has_module_scaffold_driver}，"
            f"_MODULE_SCAFFOLD_DRIVER_STACKS={sorted(cu._MODULE_SCAFFOLD_DRIVER_STACKS)}")


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


def test_detection_has_no_second_source_of_truth():
    """★P-C1（B-6）★ 栈检测**不得**再有第二份清单表——差集不是"有账"，而是**不存在**。

    本测试的前身 `test_detection_table_drift_is_accounted` 把差集钉成冻结集合
    `{requirements.txt, setup.py, Pipfile}`，并在 docstring 里显式交接："加进检测表会把
    `_should_fabricate_maven_scaffold` 对纯 python 仓从 True 翻成 False……属 B-6 范围，
    请按 B-6 的口径处置后再更新本断言"。B-6 已到，那次翻转**正是 P-C1 要的**——治前
    Django/纯 pip 工程判 unknown → 被塞 `reporting/pom.xml` → pandas/celery 走 Maven
    Central 查无 → 从契约永久剪除且每轮重解析仍是 Maven ⇒ 不可自愈。

    故断言从"差集等于某集合"升级为"**手抄表已删**"：任何人重新引入本地清单表都会红。
    """
    assert not hasattr(cu, "_MANIFEST_TO_STACK"), (
        "`_MANIFEST_TO_STACK` 又出现了——栈识别的单一事实源是 stacks/spec.py:STACK_SPEC，"
        "手抄第二份表必然漂移（P-C1 就是漂移的产物）。请走 root_manifests_by_stack() / "
        "stack_of_manifest() / stack_of_structural_manifest()。")
    assert not hasattr(cu, "_MANIFEST_TO_STACK_LC"), "同上（小写派生表亦不得复活）"


# ══════════════════════════════════════════════
# ①.5 P-C1 复核 F1：「清单不是实现证据」第四档派生视图——两个消费落点逐条对账
# ══════════════════════════════════════════════

@pytest.mark.parametrize("manifest", sorted(build_manifest_basenames()))
def test_every_build_manifest_is_not_implementation_evidence(manifest):
    """★P-C1 复核 F1★ 派生集**每一条**都必须同时被两个消费落点认成「非实现证据」。

    落点 1（`symbol_surgery._subtask_modules`）：只写 `mod/<清单>` 的纯脚手架子任务
    必须返 `{}`——否则它是符号挂靠候选 ⇒ 幻影 ownership 骗过 C1、两张皮复活
    （F1 实测手抄 5 条时 `go.mod`/`Cargo.toml`/`settings.gradle.kts`/`pyproject.toml`
    全返 `{'mod': 1}`）。
    落点 2（`contract_utils._evidence_class`）：必须分类 `manifest`——手抄 7 条时
    `settings.gradle`/`go.work` 被判 `weak_code` ⇒ Gradle/Go 聚合清单被当 flat 真源码
    参与物理根歧义判定。

    逐条 parametrize（派生自单一事实源，不手抄——新增一栈自动获得覆盖，
    [[swarm-enumeration-needs-authoritative-source]] 的正面形态）。
    """
    from swarm.brain.symbol_surgery import _subtask_modules

    st = _st("s1", create=(f"mod/{manifest}",))
    assert _subtask_modules(st) == {}, (
        f"只写 {manifest} 的脚手架子任务成了挂靠候选 ⇒ 幻影 ownership（F1）")
    assert cu._evidence_class(f"mod/{manifest}") == "manifest", (
        f"{manifest} 没被分类成 manifest ⇒ 构建清单被当真源码参与物理根判定（F1）")


def test_real_source_files_are_not_swallowed_by_the_manifest_set():
    """反向锁（防过宽）：真源码文件**不得**被第四档吞掉。

    「宁滥勿缺」的边界是清单集合本身——把 `main.py`/`App.java` 也判成 manifest 会让
    真源码子任务失去挂靠权重（该挂的挂不上）且物理根证据静默消失。哪个突变能红：
    把 `_evidence_class` 的 manifest 判定放宽成「任意文件」/把 `_subtask_modules`
    的过滤改成无条件 skip。
    """
    from swarm.brain.symbol_surgery import _subtask_modules

    st = _st("s1", create=("mod/main.py", "mod/App.java", "web/App.js"))
    assert _subtask_modules(st) == {"mod": 2, "web": 1}
    assert cu._evidence_class("mod/main.py") != "manifest"
    assert cu._evidence_class("mod/App.java") != "manifest"


def test_build_manifest_basenames_is_a_strict_superset_of_the_demote_tier():
    """集合关系锁：第四档（实现证据排除）⊋ demote 档（`structural_manifests`）。

    python 的 `pyproject.toml`/`requirements.txt` **必须在本档**（它不构成实现证据）
    而**刻意不在** demote 档（无 reconcile 路径，demote 必丢贡献）——两档后果不同，
    绝不互换（血规 10 第三条）。哪个突变能红：把 `build_manifest_basenames()`
    的实现换成复用 `structural_manifests()` 的门控循环 ⇒ python 两清单立即掉出本档。
    """
    assert structural_manifests() <= build_manifest_basenames()
    assert "pyproject.toml" in build_manifest_basenames()
    assert "requirements.txt" in build_manifest_basenames()
    assert "pyproject.toml" not in structural_manifests()  # 既有分档不被本档侵蚀


def test_symbol_surgery_and_contract_utils_share_the_derived_set():
    """★同一概念两处手抄的根治锁★ 两个落点必须读**同一个**派生视图——
    F1 的病根就是两份手抄表缺口互不相同、后果面是并集。哪个突变能红：
    任一侧改回手抄字面量集合（只要与派生集有差集即红）。

    消费契约分档（血规 10 第三条）：判定用小写集（`ss._BUILD_MANIFESTS` /
    `cu._BUILD_MANIFESTS_LC`），磁盘探测用规范大小写集（`cu._BUILD_MANIFESTS`）——
    两者都必须逐字派生自 `build_manifest_basenames()`，不许有任何增删。
    """
    from swarm.brain import symbol_surgery as ss

    derived_lc = frozenset(n.lower() for n in build_manifest_basenames())
    assert ss._BUILD_MANIFESTS == derived_lc
    assert cu._BUILD_MANIFESTS_LC == derived_lc
    assert cu._BUILD_MANIFESTS == frozenset(build_manifest_basenames())


def test_baseline_probe_queries_canonical_case_names(monkeypatch):
    """★P-C1 F1 整改 near-miss 锁★ git 树/磁盘探测必须用**规范大小写**集——
    拿小写集拼路径时 `mod/cargo.toml` 在大小写敏感 FS（CI ubuntu）上探不到真实的
    `Cargo.toml` ⇒ 既有 cargo 基线模块判不出 ⇒ 清单被当新证据，幻影 ownership 换皮复活。

    本机 APFS 大小写不敏感 ⇒ 断文件存在性零区分力（P-C1 小写化突变已踩过此坑，
    落点必须选平台无关属性），故断**查询形状**：派生集里唯一的规范大写清单是
    `Cargo.toml`/`Pipfile`，探询必须原样发出。
    """
    seen: list[str] = []
    monkeypatch.setattr(cu, "_exists_in_repo",
                        lambda pp, rel, cache, base_ref=None: seen.append(rel) or False)
    cu._is_existing_baseline_module("/fixture/repo", "mod", {}, None)
    assert "mod/Cargo.toml" in seen, "探测被换成小写集 ⇒ 大小写敏感 FS 上探不到真实清单"
    assert "mod/Pipfile" in seen
    assert "mod/cargo.toml" not in seen


@pytest.mark.parametrize("manifest,expect_stack", [
    ("requirements.txt", "python"), ("setup.py", "python"), ("Pipfile", "python"),
    ("pyproject.toml", "python"), ("pom.xml", "maven"), ("go.mod", "go"),
    ("Cargo.toml", "cargo"), ("package.json", "npm"), ("settings.gradle.kts", "gradle"),
])
def test_every_spec_root_manifest_is_recognized_on_disk(tmp_path, manifest, expect_stack):
    """★P-C1★ `spec.root_manifests` 里的**每一个**清单，磁盘上存在时都必须被认出该栈。

    逐条 parametrize 而非集合断言＝每个清单单独承重（血规：夹具的多余成员会替被测代码背书；
    一条 `for` 里任一命中即过的写法会让新漏的清单被其他清单兜住）。
    `Cargo.toml`/`Pipfile` 是**大写**的 ⇒ 本测试同时锁住"磁盘探测走规范大小写"这一档
    （小写化会让 Linux 上 `os.path.exists` 探不到，见 `root_manifests_by_stack` docstring）。
    ★F5 更正★ 原举例 `Gemfile` 不在 STACK_SPEC（ruby 是刻意的未收录栈）——
    本档真承重的大写清单只有 `Cargo.toml`/`Pipfile`。
    """
    (tmp_path / manifest).write_text("", encoding="utf-8")

    class _P:
        subtasks: list = []

    assert cu._detect_build_stack(_P(), str(tmp_path)) == expect_stack


@pytest.mark.parametrize("manifest", ["requirements.txt", "setup.py", "Pipfile",
                                      "pyproject.toml"])
def test_pure_python_repo_is_never_given_fabricated_pom(tmp_path, manifest):
    """★P-C1 病灶本体★ 纯 python 工程**绝不**被伪造 Maven 脚手架。

    治前实测：`_should_fabricate_maven_scaffold=(True,'unknown')` → 注入 `reporting/pom.xml`
    → `pandas`/`celery` 走 Maven Central 查无 → **从契约永久剪除**，且每轮重解析仍是 Maven
    ⇒ 不可自愈（27 号文 P-C1 原文）。四种 python 清单逐条锁死。
    """
    (tmp_path / manifest).write_text("", encoding="utf-8")

    class _P:
        subtasks: list = []

    should, stack = cu._should_fabricate_maven_scaffold(_P(), str(tmp_path))
    assert stack == "python"
    assert should is False, f"{manifest} 工程仍会被伪造 pom（P-C1 复发）"


def test_root_manifests_by_stack_preserves_canonical_case():
    """★P-C1 大小写档★ 磁盘探测视图必须返回**规范大小写**，不得小写化。

    ★为什么不用"造 Pipfile 再探 pipfile"来测（harness 逮到的假绿）★ 本仓开发机是 macOS
    (APFS **大小写不敏感**)，`os.path.exists('/tmp/x/pipfile')` 对 `Pipfile` 也返 True
    ⇒ 那种夹具在本机对这一维**零区分力**，只在 Linux（生产/CI）才承重。测试的承重能力
    随平台漂移＝本地永远看不出，是最坏的假绿形状。
    故把不变量提到**平台无关**的层：本视图是纯函数，"不小写化"是它自己的属性。
    真实后果（Linux 上）：小写化 ⇒ `Cargo.toml`/`Pipfile` 恒探不到 ⇒ 判 unknown ⇒ 塞 pom。
    （★F5 更正★ 原举例 `Gemfile` 不在 STACK_SPEC——ruby 是刻意的未收录栈补集。）
    """
    names = [n for n, _ in root_manifests_by_stack()]
    _mixed = [n for n in names if n != n.lower()]
    assert _mixed, ("spec 里已无任何含大写的根清单名——若确实如此，本测试失去意义；"
                    "但 `Pipfile` 在表内，出现这条断言红说明它被小写化了")
    assert "Pipfile" in names, "Pipfile 被小写化（Linux 上 os.path.exists 探不到）"


def test_root_manifests_by_stack_covers_every_spec_entry():
    """★P-C1★ 派生视图必须**逐条**覆盖 `spec.root_manifests`，一个不落。

    与上一条配对：那条锁大小写，这条锁覆盖面。突变"只出 maven"必须在这里红——
    否则"派生视图"这层可以整块残缺而只有下游行为测试报警（信号离病灶太远）。
    """
    expect = {(n, s.key) for s in STACK_SPEC.values() for n in s.root_manifests}
    assert set(root_manifests_by_stack()) == expect


@pytest.mark.parametrize("manifest", ["requirements.txt", "setup.py", "Pipfile"])
def test_plan_path_root_only_manifest_is_python_evidence(manifest):
    """★P-C1 第二个调用点（plan 路径档）★ plan 里**建** `requirements.txt` 也是 python 证据。

    这三个清单没有"整段结构区"（不像 pom 的 `<modules>`），故**不在**结构档
    `stack_of_structural_manifest`（实测该档对它们答 None，设计正确）。若只接结构档，
    greenfield python 工程（磁盘上还没有任何清单、plan 里才建）仍会判 unknown → 塞 pom。
    故本档是 `结构档 or root 档` 两问，本测试锁住那个 `or` 右侧。
    """
    plan = _plan(_st("st-1", create=[manifest, "svc/app.py"]))
    assert cu._detect_build_stack(plan, None) == "python"
    assert cu._should_fabricate_maven_scaffold(plan, None)[0] is False


def test_pc1_bare_pom_gate_recognizes_python_baseline(tmp_path):
    """★P-C1 复核 F2 升级（真 fail-open）后反转★ 裸奔闸对**任何**基线的 create-pom
    零 verify 子任务都注入栈无关断言 ①②。

    治前本测试断 `== {}`（跳过）——它锁死了"跳过"这个行为本身，从不问下游有没有人接
    （F2 实证：VALIDATE 没有那条闸，L1 对零 verify 只打 needs_review 不阻断 ⇒ 跳过＝
    零确定性验收直送 worker＝st-3-1 原病复活）。
    反转后的不变量：① `test -f` 与 ② `! grep '<version>${'` 是**栈无关**断言
    （产物存在性 + 对所创 pom 自身的字面量检查），对合法多语言新模块
    （python 根 + service-java/pom.xml）同样无害且有益 ⇒ 无条件注入；
    ③（parent 版本对账）栈相关，靠 `_root_gav` 自门控——纯 python 基线无根 pom ⇒
    自动省略（本夹具断言这一点：注入的命令里没有 parent 对账）。
    """
    from swarm.brain.plan_finisher import ensure_pom_create_min_acceptance as _gate

    (tmp_path / "requirements.txt").write_text("django==5.0\n", encoding="utf-8")
    plan = _plan(_st("st-1", create=["reporting/pom.xml"]))
    injected = _gate(plan, str(tmp_path))
    assert set(injected) == {"st-1"}, \
        "非 Maven 基线的 create-pom 零 verify 直送 worker＝st-3-1 原病复活（F2）"
    cmds = injected["st-1"]
    assert any(c.startswith("test -f reporting/pom.xml") for c in cmds), "①产物存在性未注入"
    assert any("<version>${" in c for c in cmds), "②字面量检查未注入"
    assert not any("<parent>" in c for c in cmds), \
        "纯 python 基线无根 pom ⇒ ③必须自门控省略（注入即冤杀，终闸复核 M-1）"


def test_xpom_xml_is_not_a_pom_create(tmp_path):
    """★P-C1 复核 R2-1（F1 同型误命中的收口）★ pom 判定必须 **basename 相等**——
    `endswith("pom.xml")` 会把 `xpom.xml` 误判为 pom create，注入 `test -f`/
    `! grep '<version>${'` 等 Maven 专属断言给非 pom 产物虚假背书。
    """
    from swarm.brain.plan_finisher import ensure_pom_create_min_acceptance as _gate

    plan = _plan(_st("st-1", create=["module/xpom.xml"]))
    assert _gate(plan, str(tmp_path)) == {}, \
        "xpom.xml 不是 pom create——同后缀误命中必须收口（R2-1，与 F1 同原理）"
    # 对照方向：真 pom.xml 仍注入（防「整条闸删掉」式突变拿本测试当假绿）
    plan2 = _plan(_st("st-2", create=["module/pom.xml"]))
    assert set(_gate(plan2, str(tmp_path))) == {"st-2"}, "真 pom.xml 必须仍注入①②"


def test_negated_grep_rewrite_does_not_touch_xpom_xml():
    """★R2-1 同型第二现场★ `sanitize_negated_grep_exam` 的 pom 判定同口径收口——
    对 xpom.xml 的负断言不得被重写成 `<artifactId>` 锚定形（误分类）；真 pom.xml 仍重写。"""
    from swarm.brain.plan_finisher import sanitize_negated_grep_exam as _san

    plan = _plan(_st("st-1", create=[]))
    plan.subtasks[0].harness.verify_commands = [
        "! grep -q 'lombok' module/xpom.xml",
        "! grep -q 'lombok' module/pom.xml",
    ]
    _san(plan)
    vcs = plan.subtasks[0].harness.verify_commands
    assert vcs[0] == "! grep -q 'lombok' module/xpom.xml", \
        "xpom.xml 不是 pom——不得按 pom 语义重写（R2-1）"
    assert "<artifactId>lombok</artifactId>" in vcs[1], "真 pom.xml 必须仍被锚定重写"


def test_sync_manifest_names_derives_from_the_single_source():
    """★P-C1 复核 R2-2★ `_SYNC_MANIFEST_NAMES` 基础集必须从 `build_manifest_basenames()`
    派生（#16「加一栈=加一条」的承诺到达 sync 层），extras（lockfile/ant/tsconfig）是
    消费契约刻意的显式附加——两层都锁，防手抄表漂移回潮。"""
    from swarm.stacks import build_manifest_basenames
    from swarm.worker.executor_sync import _SYNC_MANIFEST_EXTRA, _SYNC_MANIFEST_NAMES

    assert frozenset(_SYNC_MANIFEST_NAMES) >= build_manifest_basenames(), \
        f"sync 层漏了 spec 里的清单: {sorted(build_manifest_basenames() - frozenset(_SYNC_MANIFEST_NAMES))}"
    # extras 逐字锁：每件都必须是被审过的刻意附加（增删要过这条，别随手塞）
    assert frozenset(_SYNC_MANIFEST_EXTRA) == frozenset(
        {"build.xml", "go.sum", "Cargo.lock", "tsconfig.json"})
    # 派生承诺的实证：spec 有而旧手抄表漏的 Pipfile 现在必须到场
    assert "Pipfile" in _SYNC_MANIFEST_NAMES


def test_unknown_stack_fallback_is_loud(tmp_path, caplog):
    """★P-C1 LOUD（血规 3）★ `unknown → 回退 Maven` 是降级，必须至少一次 WARNING。

    治前全程零日志：`_detect_build_stack` 的 `return "unknown"` 注释写着"调用方 log"，而
    两个调用点都没 log（`plan_finisher:583` 连栈值都丢弃 `_maven_scaffold_ok, _ =`）
    ⇒ 承诺没兑现，php/ruby 工程被塞 pom **无声**。

    告警打在 `_should_fabricate_maven_scaffold` **函数内**而非各调用点，故本测试同时是
    "单一权威闸名副其实"的锁——新增调用点自动获得告警，不会"补一个漏一个"（血规 10 第一条）。
    """
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")   # php：整栈未收录

    class _P:
        subtasks: list = []

    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        should, stack = cu._should_fabricate_maven_scaffold(_P(), str(tmp_path))
    assert (should, stack) == (True, "unknown")      # 行为保持 back-compat
    _warns = [r for r in caplog.records if r.levelno >= logging.WARNING
              and "P-C1" in r.getMessage()]
    assert _warns, "unknown 回退 Maven 无 WARNING（降级不可观测＝血规 3 违反）"


def test_ambiguous_mixed_stack_unknown_is_distinguishable_from_no_evidence(caplog):
    """★P-C1 自查整改★ 两种 unknown 必须**机读可辨**，不许塌成同一个信号。

    `unknown` 只有两个来源：①零清单证据（greenfield 或未收录栈如 php/ruby）；②歧义混栈
    护栏刻意回退（异栈清单 + JVM 源码并存，防 round62 家族级回归）。二者**处置完全不同**——
    ①要么正常要么该给 STACK_SPEC 加条目，②是设计如此不需要动。

    自查发现的问题：整改前那条 WARNING 单说"栈证据为空……请给 STACK_SPEC 加条目"，而混栈
    这条路径证据其实**充足**（requirements.txt + .java 都在）⇒ 日志把读者指向 php/ruby
    方向，真因却是"这个 plan 同时像两个栈"。现由 `_detect_build_stack` 对②自打一条 INFO。
    """
    plan = _plan(_st("st-1", create=["requirements.txt", "svc/src/main/java/A.java"]))
    with caplog.at_level(logging.INFO, logger="swarm.brain.contract_utils"):
        should, stack = cu._should_fabricate_maven_scaffold(plan, None)
    # 行为不变：歧义混栈仍保守回退 Maven（round62 防线，绝不因 P-C1 松动）
    assert (should, stack) == (True, "unknown")
    # ★按**机读键**判而非散文子串★ 外层 WARNING 并列两因，散文里也含"歧义混栈"四字 ⇒ 子串
    # 匹配把两个信号又混成一个（本测试第一版就这么写的，反向锁当场抓到＝假探针宽度）。
    assert [r for r in caplog.records
            if "stack_unknown_cause=ambiguous_mixed" in r.getMessage()], \
        "歧义混栈回退没有自己的机读键 ⇒ 与'零证据'不可辨（血规 10 第四条）"


def test_no_evidence_unknown_does_not_claim_mixed_stack(caplog):
    """★反向锁★ 零证据那条**不得**打歧义混栈 INFO——否则该信号恒亮＝零信息量。

    与上一条配对（always-emit 一族）：只验"混栈会打"会让"无条件打"的实现也全绿。
    """
    plan = _plan(_st("st-1", create=["README.md"]))       # 零清单、零 JVM 源
    with caplog.at_level(logging.INFO, logger="swarm.brain.contract_utils"):
        assert cu._should_fabricate_maven_scaffold(plan, None) == (True, "unknown")
    assert not [r for r in caplog.records
                if "stack_unknown_cause=ambiguous_mixed" in r.getMessage()], \
        "零证据也带歧义混栈机读键（粘滞信号）"


def test_known_stack_does_not_emit_unknown_warning(tmp_path, caplog):
    """★反向锁★ 已知栈**不得**触发 P-C1 告警——否则告警变噪声，没人再读它。

    与上一条配对：只验"unknown 会告警"会让"恒告警"的实现也全绿（粘滞告警＝等于没有告警，
    复核盲区清单 always-emit 一族）。
    """
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")

    class _P:
        subtasks: list = []

    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        cu._should_fabricate_maven_scaffold(_P(), str(tmp_path))
    assert not [r for r in caplog.records if "P-C1" in r.getMessage()], \
        "Maven 工程也打 P-C1 降级告警（粘滞告警）"


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
