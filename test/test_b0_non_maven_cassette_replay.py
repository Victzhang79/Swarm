#!/usr/bin/env python3
"""B-0 末件：**非 Maven cassette 离线重放**（27 号文 §7 B-0 第 5 条）。

## 为什么必须有这条

27 号文 §4.3 实测：`cassettes/` 里 22 份 plan 快照 **100% Maven**、210 条 LLM 录像被测项目
全是 RuoYi，**零非 Maven 样本**。后果不是"少测一点"，而是：

> 离线重放对新驱动层**零证明力**，且**静默通过**。

也就是说 B-2~B-6 的每一批都可以拿 `cassette_replay` 跑出一片绿，而它跑的全是 Maven 路径。
本文件把 npm workspaces / go.work 两份**合成** cassette 接进同一个 `replay_cassette`
（与 live 的 plan→elaborate 真实调用序逐 pass 同构），让重放回路对异栈**真的有证明力**。

## 合成 ≠ 现场（诚实边界）

`cassettes/` 里 run18/run19 是真实 live 现场固化，**本文件的两份不是**。它们把
RUN19/round62 的**结构**（多写者争抢根聚合清单 + 模块清单脚手架 + 一批源码挤进单发路径）
平移到 npm/go 布局。真实非 Maven E2E 基线项目（用户已明确降优先级）才能补上"形态更杂"
那一层——本文件不冒充它。

工程树复用 B-0 共享夹具（`conftest` 的 `make_workspace`）：replay 会读磁盘做
aggregate-vs-新建分流、模板取证，微型树走不进那些分支。

## ★本文件落地当场抓到的两条（实测，非推演）——均已治（B-6）★

- **N-2b** go.work 多模块仓：`_inject_go_scaffolds` 只认**根** `go.mod` 推导 import 路径，
  根上只有 `go.work` 时**整栈零脚手架**（`scaffolds=[]`）。而 module path 本可从兄弟
  `go.mod` 或 `go.work` 的 `use` 确定性推出——不是"推不出"，是**没接这条证据源**。
  治：`_go_module_path_prefix` 两条证据源（根 go.mod → 工作区成员反推），互斥前缀 → 歧义
  fail-closed（绝不挑边臆造 module 路径）。同批 `_go_root_directive` 补读 `go.work` 的
  `go` 指令（go.work 仓恒落 '1.21' 兜底会低于工作区真值 → `go.work requires go >= 1.22`）。
- **N-3** 规则5 三处**各自**写死 `f"{mod}/pom.xml"`：npm driver 已把清单建出、依赖落地、
  验收挂上，它们**仍报每个模块"依赖契约无 pom owner 承接"** → VALIDATE_PLAN 刷假警报
  （`result.warn`，**不阻断**，故是噪声不是死锁）。治：`_rule5_manifests(stack)` 单一事实源
  + `_module_manifest_candidates`（补 R57-1 物理落点：契约标签 `alarm` 的包真身在
  `packages/alarm/package.json`，只按标签找恒 miss）。**两个消费者后果不同 → 参数显式分档**：
  告警面（plan_validator）传 stack+dirs 要最宽认定；注入面（`inject_build_scaffold_subtasks`，
  Maven 专属）不传，维持逐字今日行为——那一侧漏报=该模块没人建构建文件=整模块编译失败。
  原登记里"异栈能进 driver 只因无 pom owner 恒真（歪打正着）"这句**不准确**：npm/go driver
  走的是 `_contract_dep_entries`，在 `_should_fabricate_maven_scaffold` 分流处就已早返，
  从不经过 `unclaimed_contract_deps`（`test_npm_go_driver_does_not_depend_on_unclaimed` 钉住）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_s_bs = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_m_bs = importlib.util.module_from_spec(_s_bs)
_s_bs.loader.exec_module(_m_bs)

# scripts/ 不是包——按路径加载（与 test_cassette_replay.py 同款既有范式）。
_rp = Path(__file__).resolve().parent.parent / "scripts" / "cassette_replay.py"
_s_rp = importlib.util.spec_from_file_location("cassette_replay", _rp)
cassette_replay = importlib.util.module_from_spec(_s_rp)
sys.modules["cassette_replay"] = cassette_replay
_s_rp.loader.exec_module(cassette_replay)

from swarm.brain.plan_validator import validate_plan_structure  # noqa: E402
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan  # noqa: E402

SCHEMA = "swarm-plan-cassette/v1"


def _st(sid, create=None, writable=None):
    return SubTask(id=sid, description=f"task {sid}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=writable or []),
                   acceptance_criteria=["ok"])


# ★cassette 载荷只有一份：入库生成器本身★（双路复核 MEDIUM-4/MEDIUM-2 整改）
#
# 此前树共用 builders，但 plan / file_plan / shared_contract 在脚本与测试里**各写一份**
# （`description`/`difficulty`/`readable` 都不同，而 description 是多个确定性 pass 的输入）
# → 正是 R-1 那个"两份名单互为对方反证"的形状，且**等价性无锁**：B-5/B-6 让脚本产出的
# cassette 无法重放时，全量照旧全绿，而那个脚本是 runbook 里让人跑的那条回路、也是唯一
# 持久件（`cassettes/` 已 gitignore）。现在测试直接 import 它 → "能生成→能重放→断言成立"
# 一条链锁死。加载范式与本文件加载 cassette_replay.py 同款（scripts/ 不是包）。
_synth_path = Path(__file__).resolve().parent.parent / "scripts" / "cassette_synth_non_maven.py"
_s_synth = importlib.util.spec_from_file_location("cassette_synth_non_maven", _synth_path)
synth = importlib.util.module_from_spec(_s_synth)
sys.modules["cassette_synth_non_maven"] = synth
_s_synth.loader.exec_module(synth)


@pytest.fixture
def npm_cassette(tmp_path):
    """npm workspaces：两个子任务各建一个包并**都注册进根 `package.json`**（R-1 的 npm 侧）。"""
    cassette, fx = synth._npm(tmp_path / "npm_workspaces")
    return fx, cassette


@pytest.fixture
def go_cassette(tmp_path):
    """go.work：两个子任务各建一个模块并**都注册进 `go.work`**（R-1 的 go 侧）。"""
    cassette, fx = synth._go(tmp_path / "go_work")
    return fx, cassette


def test_synth_generator_and_replay_agree_on_the_schema():
    """生成器产出的 cassette 必须与 `cassette_extract.py` 的落盘键集同构。

    生成器是唯一持久件（`cassettes/` gitignore），schema 漂移了没人会知道——除了这条。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        for fn in (synth._npm, synth._go):
            cassette, _fx = fn(Path(td) / fn.__name__.strip("_"))
            for key in ("schema", "task_id", "project_path", "base_commit",
                        "plan", "shared_contract", "file_plan", "task_description"):
                assert key in cassette, f"{fn.__name__} 缺 cassette 键 {key}"
            assert cassette["schema"] == SCHEMA
            assert "_synthetic" in cassette, "合成性质必须机读可辨，别只写在散文里"
            assert Path(cassette["project_path"]).is_dir(), "project_path 必须指向真实树"


# ══════════════════════════════════════════════
# ① 重放回路对异栈真的跑得通（不崩、不留 Maven 产物）
# ══════════════════════════════════════════════

def test_npm_cassette_replays_offline_without_maven_leakage(npm_cassette):
    """npm cassette 走完 replay 全序：不崩 + 结构合法 + **一个 pom.xml 都不许出现**。

    L4/L8 血泪（Maven 产物泄漏到异栈）是本仓反复复发的一族；重放序里还夹着两个
    Maven 专属 pass（`ensure_pom_create_min_acceptance` / `wire_module_pom_dep_edges`），
    它们在异栈上必须**安静地 no-op**，而不是造出幻影 pom。
    """
    fx, cassette = npm_cassette
    res = cassette_replay.replay_cassette(cassette, verbose=True)

    assert res.ok, f"npm cassette 重放崩在 {res.failed_stage}:\n{res.traceback_str}"
    assert res.failed_stage is None

    assert {(s["module"], s["stack"]) for s in res.scaffolds} == {
        ("alarm", "npm"), ("notify", "npm")}, f"npm driver 未接上，实得 {res.scaffolds}"

    created = [f for st in res.plan.subtasks
               for f in (list(st.scope.create_files) + list(st.scope.writable))]
    assert not [f for f in created if f.rsplit("/", 1)[-1] == "pom.xml"], (
        f"Maven 产物泄漏进 npm 工程（L4/L8 复发）：{created}")
    assert "packages/alarm/package.json" in created
    assert "packages/notify/package.json" in created

    # 两个 Maven 专属 pass 必须安静 no-op（不是崩、也不是造 pom）
    assert not res.b3_pom_min_exam, f"pom 裸奔闸在 npm 上不该有产出：{res.b3_pom_min_exam}"
    assert not res.b4_module_pom_edges, f"模块 pom 依赖边在 npm 上不该有产出：{res.b4_module_pom_edges}"

    r = validate_plan_structure(res.plan)
    assert r.valid, f"重放后 plan 必须结构合法：{r.issues}"


def test_go_cassette_replays_offline_without_maven_leakage(go_cassette):
    """go.work cassette 同上。**注意期望值刻意不含脚手架**——见 N-2b。"""
    fx, cassette = go_cassette
    res = cassette_replay.replay_cassette(cassette, verbose=True)

    assert res.ok, f"go cassette 重放崩在 {res.failed_stage}:\n{res.traceback_str}"
    created = [f for st in res.plan.subtasks
               for f in (list(st.scope.create_files) + list(st.scope.writable))]
    assert not [f for f in created if f.rsplit("/", 1)[-1] == "pom.xml"], (
        f"Maven 产物泄漏进 go 工程：{created}")
    assert not res.b3_pom_min_exam and not res.b4_module_pom_edges

    r = validate_plan_structure(res.plan)
    assert r.valid, f"重放后 plan 必须结构合法：{r.issues}"


# ══════════════════════════════════════════════
# ② R-1 收敛闭环走完整重放序（不只是 resolve 单函数）
# ══════════════════════════════════════════════

@pytest.mark.parametrize("which", ["npm", "go"])
def test_r1_convergence_survives_the_whole_replay_sequence(which, npm_cassette, go_cassette):
    """R-1 在**全序**（normalize→inject→decouple→resolve→四个 B3 pass）后仍收敛。

    ★与 `test_b3_stack_spec_single_source.py` / 矩阵档的差别★ 那两处跑的是单个/两个函数；
    R-1 的死状是"规划期硬闸永不收敛 → 同签名两轮 → 熔断"，而**收敛与反收敛可以出在不同
    pass**（round62 就是 inject 造边、decouple 删边）。只有跑完整序才排除"后面某个 pass
    把前面收敛好的又拆散"。
    """
    fx, cassette = npm_cassette if which == "npm" else go_cassette
    agg = fx.aggregate_manifest

    pre = TaskPlan.model_validate(cassette["plan"])
    before = validate_plan_structure(pre)
    assert not before.valid, (
        f"{which}: 根聚合清单 {agg} 双写者未被判死（判死名单漏了它）：{before.issues}")
    # ★必须断【根聚合闸】的原话，不能只断 "issues 里提到了 agg"★——通用"并行同写"闸的
    # 文案里也含文件名，于是那种宽松断言对"名单漏判"零区分力（突变 M10 实证：把
    # package.json 从名单摘掉，宽松断言仍全绿）。上一批 F-7 同型。
    assert any(f"根聚合清单 {agg}" in i for i in before.issues), (
        f"{which}: 判死了但**不是根聚合闸**判的（通用写者闸假过）：{before.issues}")

    res = cassette_replay.replay_cassette(cassette)
    assert res.ok, f"{which}: 重放崩在 {res.failed_stage}:\n{res.traceback_str}"

    after = validate_plan_structure(res.plan)
    assert after.valid, (
        f"{which}: 全序跑完仍不收敛 → 同签名两轮 → 熔断 fail-fast（R-1 死锁）：{after.issues}")
    writers = [st.id for st in res.plan.subtasks
               if agg in (list(st.scope.writable) + list(st.scope.create_files))]
    assert len(writers) == 1, f"{which}: 收敛后仍有多个 {agg} 写者 {writers}"


# ══════════════════════════════════════════════
# ③ N-2b / N-3：抓到就钉死（xfail strict，不留待办注释）
# ══════════════════════════════════════════════

def test_go_work_modules_get_manifest_scaffolds(go_cassette):
    """go.work 多模块仓的新模块**也该拿到 `go.mod` 脚手架**（N-2b，已治）。

    module path 并非推不出：兄弟 `auth/go.mod` 写着 `example.com/app/auth`、`go.work` 的
    `use` 也列着成员——确定性证据就在磁盘上，只是没接这条源。治前=跳过整栈脚手架
    → 回到 R47/R53 病（派 worker 手写清单 + 臆造版本），正是 L4 说的"栈中立≠一律跳过"。
    """
    fx, cassette = go_cassette
    res = cassette_replay.replay_cassette(cassette)
    assert res.ok
    mods = {s["module"] for s in res.scaffolds}
    assert {"billing", "report"} <= mods, (
        f"go.work 多模块仓零脚手架（N-2b），实得 {res.scaffolds}")
    # 前缀必须来自**兄弟证据**（`example.com/app`），不是某个兜底常量：错前缀 = import 全仓
    # 对不上，且盖着"权威模板"章发给 worker（R47 血泪）。
    descs = {s["module"]: st.description for s in res.scaffolds
             for st in res.plan.subtasks if st.id == s["subtask_id"]}
    for m in ("billing", "report"):
        assert f"module example.com/app/{m}" in descs[m], (
            f"{m} 的 go.mod 模板 module 行不是从兄弟 go.mod 推出的前缀：{descs[m]}")


def test_unclaimed_contract_deps_is_stack_aware(npm_cassette):
    """npm driver 把清单建出、依赖落地、验收挂上之后，规则5 机读面**不该**再报落空（N-3，已治）。

    治前 `unclaimed_contract_deps` 只找 `f"{mod}/pom.xml"` → 异栈恒"无 owner" →
    VALIDATE_PLAN 每个模块刷一条假警报。方向是**误报**（`result.warn` 不阻断），所以是
    噪声污染而非死锁。

    ★这条测试的两个断言是【一对】，缺一即假绿★ 只断"stack-aware 调用返回空"会被"把
    `unclaimed_contract_deps` 改成无条件返 []"通过；第二个断言（Maven 口径仍报落空）钉住
    "空是**因为按 npm 口径找到了 owner**"，而不是因为整个函数被拧成了 fail-open。
    """
    from swarm.brain.contract_utils import _resolve_module_dirs, unclaimed_contract_deps

    fx, cassette = npm_cassette
    res = cassette_replay.replay_cassette(cassette)
    assert res.ok
    created = [f for st in res.plan.subtasks for f in st.scope.create_files]
    assert "packages/alarm/package.json" in created, "前提：npm 清单确已被建出"
    dirs, _, _ = _resolve_module_dirs(res.plan, cassette["project_path"])
    assert unclaimed_contract_deps(res.plan, stack="npm", dirs=dirs) == [], (
        "清单已建、依赖已落地，规则5 机读面仍报落空（N-3 假警报）")
    assert unclaimed_contract_deps(res.plan) != [], (
        "缺省（Maven）口径也返空 ⇒ 上一条的绿不是'按 npm 口径找到 owner'，"
        "而是函数被拧成了无条件 fail-open（注入面靠它决定给谁建构建文件，漏报=整模块编译失败）")
