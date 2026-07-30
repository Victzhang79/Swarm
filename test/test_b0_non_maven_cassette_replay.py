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

## ★本文件落地当场抓到的两条（实测，非推演）★

- **N-2b** go.work 多模块仓：`_inject_go_scaffolds` 只认**根** `go.mod` 推导 import 路径，
  根上只有 `go.work` 时**整栈零脚手架**（`scaffolds=[]`）。而 module path 本可从兄弟
  `go.mod` 或 `go.work` 的 `use` 确定性推出——不是"推不出"，是**没接这条证据源**。
  归 B-6（DependencyResolver / 脚手架 driver）。
- **N-3** `unclaimed_contract_deps` 写死 `f"{mod}/pom.xml"`：npm driver 已把清单建出、
  依赖落地、验收挂上，它**仍报每个模块"依赖契约无 pom owner 承接"** → VALIDATE_PLAN
  刷假警报（`result.warn`，**不阻断**，故是噪声不是死锁）。另有隐性耦合：
  `inject_build_scaffold_subtasks` 拿它当**入口清单**，异栈能进 driver 只因"无 pom owner"
  对非 Maven 模块恒真（歪打正着）——将来谁把它改成栈感知却漏改注入入口，npm/go 脚手架会
  **静默停摆**。归 B-6。
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


def _cassette(task_id: str, root: Path, plan: TaskPlan, file_plan: list[dict]) -> dict:
    """落成与 `cassette_extract.py` 逐键同构的 cassette（好让它能直接落盘手动重放）。"""
    return {
        "schema": SCHEMA,
        "task_id": task_id,
        "thread_id": None,
        "project_id": "b0-synthetic",
        "project_path": str(root),
        "base_commit": None,
        "plan": plan.model_dump(mode="json"),
        "shared_contract": plan.shared_contract,
        "file_plan": file_plan,
        "task_description": f"★合成夹具（非真实 E2E 现场）★ {task_id}",
    }


@pytest.fixture
def npm_cassette(make_workspace):
    """npm workspaces：两个子任务各建一个包并**都注册进根 `package.json`**（R-1 的 npm 侧）。"""
    fx = make_workspace("npm_workspaces")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["packages/alarm/src/index.ts",
                            "packages/alarm/src/rule.ts",
                            "packages/alarm/src/dispatch.ts"], writable=["package.json"]),
        _st("st-2", create=["packages/notify/src/index.ts"], writable=["package.json"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm", "artifacts": ["axios"]},
        {"module": "notify", "artifacts": ["alarm"]},   # 内部包 → workspace:*
    ]}
    return fx, _cassette("synthetic-npm-workspaces", fx.root, plan, [
        {"module": "alarm", "path": "packages/alarm/src/index.ts"},
        {"module": "notify", "path": "packages/notify/src/index.ts"},
    ])


@pytest.fixture
def go_cassette(make_workspace):
    """go.work：两个子任务各建一个模块并**都注册进 `go.work`**（R-1 的 go 侧）。"""
    fx = make_workspace("go_work")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["billing/handler.go", "billing/repo.go",
                            "billing/model.go"], writable=["go.work"]),
        _st("st-2", create=["report/handler.go"], writable=["go.work"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "billing", "artifacts": ["github.com/gin-gonic/gin"]},
        {"module": "report", "artifacts": ["billing"]},
    ]}
    return fx, _cassette("synthetic-go-work", fx.root, plan, [
        {"module": "billing", "path": "billing/handler.go"},
        {"module": "report", "path": "report/handler.go"},
    ])


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

@pytest.mark.xfail(strict=True, reason="N-2b（B-6）：_inject_go_scaffolds 只认根 go.mod，"
                                       "go.work 多模块仓 → 整栈零脚手架")
def test_go_work_modules_get_manifest_scaffolds(go_cassette):
    """go.work 多模块仓的新模块**也该拿到 `go.mod` 脚手架**。

    module path 并非推不出：兄弟 `auth/go.mod` 写着 `example.com/app/auth`、`go.work` 的
    `use` 也列着成员——确定性证据就在磁盘上，只是没接这条源。当前行为=跳过整栈脚手架
    → 回到 R47/R53 病（派 worker 手写清单 + 臆造版本），正是 L4 说的"栈中立≠一律跳过"。
    """
    fx, cassette = go_cassette
    res = cassette_replay.replay_cassette(cassette)
    assert res.ok
    mods = {s["module"] for s in res.scaffolds}
    assert {"billing", "report"} <= mods, (
        f"go.work 多模块仓零脚手架（N-2b），实得 {res.scaffolds}")


@pytest.mark.xfail(strict=True, reason="N-3（B-6）：unclaimed_contract_deps 写死 mod/pom.xml，"
                                       "npm 清单已建仍报落空 → VALIDATE_PLAN 假警报")
def test_unclaimed_contract_deps_is_stack_aware(npm_cassette):
    """npm driver 把清单建出、依赖落地、验收挂上之后，规则5 机读面**不该**再报落空。

    当前 `unclaimed_contract_deps` 只找 `f"{mod}/pom.xml"` → 异栈恒"无 owner" →
    VALIDATE_PLAN 每个模块刷一条假警报。方向是**误报**（`result.warn` 不阻断），所以是
    噪声污染而非死锁——但它同时是 `inject_build_scaffold_subtasks` 的**入口清单**，
    改它必须同步改注入入口，否则 npm/go 脚手架静默停摆。
    """
    from swarm.brain.contract_utils import unclaimed_contract_deps

    fx, cassette = npm_cassette
    res = cassette_replay.replay_cassette(cassette)
    assert res.ok
    created = [f for st in res.plan.subtasks for f in st.scope.create_files]
    assert "packages/alarm/package.json" in created, "前提：npm 清单确已被建出"
    assert unclaimed_contract_deps(res.plan) == [], (
        "清单已建、依赖已落地，规则5 机读面仍报落空（N-3 假警报）")
