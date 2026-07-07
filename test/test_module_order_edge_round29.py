"""round29 A 治本：模块「注册先于脚手架」依赖序（task d37a52a3 级联 abandon 真根因）。

根因链三层：
1. plan 期 contract_utils 规则 4 把边连反——「建脚手架」depends_on「注册模块」→ 注册先落地、
   模块目录还不存在 → Maven `Child module .../alarm-interface does not exist` 毒化 reactor。
2. worker L1 分类器不识别该症状 → 落泛化能力失败 → 烧重试。
3. failure.py replan 守卫（有成功兄弟绝不全量 replan）堵死回 PLAN → 级联 abandon（+9/+10/+24）。

治本（对应三层）：
(a) worker/l1_pipeline 新分类 `_build_error_is_reactor_missing_module` → BLOCKED +
    pipeline_blocked="module_registered_before_scaffold" + 结构化 blocked_on_modules。
(c) contract_utils 规则 4 反正边方向：registrant-after-scaffold（删反向、单一规范方向）。
(b) failure.py 定点重排阶梯：撞该症状 → 插规范边 + 只重派失败、保兄弟、断路器 → retry。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import subprocess

from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan, WorkerOutput


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path, files: dict[str, str]) -> str:
    proj = tmp_path / "proj"
    proj.mkdir()
    for rel, content in files.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return str(proj)


def _st(sid, *, writable=None, create=None, readable=None, depends=None):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(
            writable=writable or [], create_files=create or [], readable=readable or [],
        ),
        harness=TaskHarness(language="java"),
        depends_on=depends or [],
    )


_ROOT_POM = (
    "<project>\n"
    "  <modules>\n"
    "    <module>ruoyi-admin</module>\n"
    "  </modules>\n"
    "</project>\n"
)


# ═════════════════ (a) worker 分类器 ═════════════════

def test_reactor_missing_module_classifier_maven():
    from swarm.worker.l1_pipeline import _build_error_is_reactor_missing_module

    # Maven 现场原文（task d37a52a3）
    out1 = (
        "[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n"
        "[FATAL] Child module /workspace/alarm-interface/pom.xml of /workspace/pom.xml "
        "does not exist @\n"
    )
    assert _build_error_is_reactor_missing_module(out1) == {"alarm-interface"}

    out2 = "[ERROR] Could not find the selected project in the reactor: alarm-interface @\n"
    assert _build_error_is_reactor_missing_module(out2) == {"alarm-interface"}

    # 坐标形式 `:artifactId` 也取模块名
    out3 = "[ERROR] Could not find the selected project in the reactor: :alarm-sdk\n"
    assert _build_error_is_reactor_missing_module(out3) == {"alarm-sdk"}


def test_reactor_missing_module_classifier_other_stacks_and_negatives():
    from swarm.worker.l1_pipeline import _build_error_is_reactor_missing_module

    # Gradle settings include 缺目录
    g = "Project directory '/workspace/feature-x' does not exist.\n"
    assert _build_error_is_reactor_missing_module(g) == {"feature-x"}
    # Cargo workspace member 缺
    c = "error: failed to load manifest for workspace member `/workspace/crates/util`\n"
    assert _build_error_is_reactor_missing_module(c) == {"crates/util"}
    # 普通编译错误 / infra 错误 → 不误报
    assert _build_error_is_reactor_missing_module(
        "[ERROR] cannot find symbol: class Foo") == set()
    assert _build_error_is_reactor_missing_module("Connection refused") == set()
    assert _build_error_is_reactor_missing_module("") == set()
    assert _build_error_is_reactor_missing_module(None) == set()


def test_reactor_missing_module_multi_and_coordinate():
    """双复核整改：逗号多模块列表不得静默丢第二个；maven 坐标取 artifactId 而非 groupId。"""
    from swarm.worker.l1_pipeline import _build_error_is_reactor_missing_module

    multi = "[ERROR] Could not find the selected project in the reactor: ruoyi-alarm, ruoyi-sdk @\n"
    assert _build_error_is_reactor_missing_module(multi) == {"ruoyi-alarm", "ruoyi-sdk"}
    coord = "[ERROR] Could not find the selected project in the reactor: com.ruoyi:alarm-sdk\n"
    assert _build_error_is_reactor_missing_module(coord) == {"alarm-sdk"}
    # Go：go.work 在 does not exist 之前的真实措辞顺序也要命中
    go = "go: directory ./crates/util listed in go.work does not exist\n"
    assert "crates/util" in _build_error_is_reactor_missing_module(go)


# ═════════════════ (c) plan 期规则 4 方向 ═════════════════

def test_rule4_registrant_after_scaffold(tmp_path):
    """registrant(root pom owner) 必须依赖 scaffold（注册后于脚手架），而非反向。"""
    from swarm.brain.contract_utils import normalize_plan_scopes

    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM, "ruoyi-admin/pom.xml": "<project/>"})
    st_reg = _st("st-1", writable=["pom.xml"])                       # 注册模块（root pom owner）
    st_scaf = _st("st-6", create=["alarm-interface/pom.xml",
                                  "alarm-interface/src/A.java"])     # 脚手架
    plan = TaskPlan(subtasks=[st_reg, st_scaf])

    normalize_plan_scopes(plan, project_path=proj)

    reg = next(s for s in plan.subtasks if s.id == "st-1")
    scaf = next(s for s in plan.subtasks if s.id == "st-6")
    assert "st-6" in (reg.depends_on or []), (
        f"registrant 必须依赖 scaffold（注册后于脚手架落地），实际 reg.depends_on={reg.depends_on}"
    )
    assert "st-1" not in (scaf.depends_on or []), (
        f"scaffold 绝不能依赖 registrant（正是 d37a52a3 的反边），实际 scaf.depends_on={scaf.depends_on}"
    )


def test_rule4_removes_preexisting_reverse_edge_no_2cycle(tmp_path):
    """plan 自带 scaffold→registrant 反边时，规则 4 须删反边、只留单一规范方向（不留 2-cycle）。"""
    from swarm.brain.contract_utils import normalize_plan_scopes
    from swarm.brain.plan_batch import break_dependency_cycles

    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    st_reg = _st("st-1", writable=["pom.xml"])
    st_scaf = _st("st-6", create=["alarm-interface/pom.xml"], depends=["st-1"])  # 反边预置
    plan = TaskPlan(subtasks=[st_reg, st_scaf])

    normalize_plan_scopes(plan, project_path=proj)

    reg = next(s for s in plan.subtasks if s.id == "st-1")
    scaf = next(s for s in plan.subtasks if s.id == "st-6")
    assert "st-6" in (reg.depends_on or [])
    assert "st-1" not in (scaf.depends_on or []), "反向边必须被删除，否则 2-cycle 被环卫随机断"
    # 过环卫后规范边确定性存活（环卫消费 dict 形态）
    cleaned = break_dependency_cycles(
        [{"id": s.id, "depends_on": list(s.depends_on or [])} for s in plan.subtasks]
    )
    reg_d = next(d for d in cleaned if d["id"] == "st-1")
    assert "st-6" in reg_d["depends_on"], "规范边必须在 break_dependency_cycles 后确定性存活"


def test_rule4_content_task_still_after_registrant(tmp_path):
    """非脚手架的模块内容子任务仍依赖 registrant（内容 -pl 构建需注册在位），链式 content→reg→scaf。"""
    from swarm.brain.contract_utils import normalize_plan_scopes

    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    st_reg = _st("st-1", writable=["pom.xml"])
    st_scaf = _st("st-6", create=["alarm-interface/pom.xml"])
    st_content = _st("st-7", create=["alarm-interface/src/main/java/B.java"])
    plan = TaskPlan(subtasks=[st_reg, st_scaf, st_content])

    normalize_plan_scopes(plan, project_path=proj)

    reg = next(s for s in plan.subtasks if s.id == "st-1")
    content = next(s for s in plan.subtasks if s.id == "st-7")
    assert "st-6" in (reg.depends_on or [])
    assert "st-1" in (content.depends_on or []), "内容子任务仍应后于注册（链式传递到脚手架之后）"


def test_rule4_owner_backstop_is_scaffold_no_self_edge(tmp_path):
    """无独立 registrant 时 owner backstop=首个建模块 pom 者（自己就是脚手架）→ 不得自环。"""
    from swarm.brain.contract_utils import normalize_plan_scopes

    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    st_scaf = _st("st-6", create=["alarm-interface/pom.xml"])
    st_other = _st("st-7", create=["alarm-interface/src/A.java"])
    plan = TaskPlan(subtasks=[st_scaf, st_other])

    normalize_plan_scopes(plan, project_path=proj)

    scaf = next(s for s in plan.subtasks if s.id == "st-6")
    assert "st-6" not in (scaf.depends_on or []), "owner=scaffold 自身时绝不能自环"


def test_rule4_nested_module_gets_order_edge(tmp_path):
    """猎人#5 整改：嵌套模块（backend/service-a/pom.xml）不得被规则 4 无视（零序约束）。"""
    from swarm.brain.contract_utils import normalize_plan_scopes

    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    st_reg = _st("st-1", writable=["pom.xml"])
    st_scaf = _st("st-6", create=["backend/service-a/pom.xml"])
    plan = TaskPlan(subtasks=[st_reg, st_scaf])
    normalize_plan_scopes(plan, project_path=proj)
    reg = next(s for s in plan.subtasks if s.id == "st-1")
    scaf = next(s for s in plan.subtasks if s.id == "st-6")
    assert "st-6" in (reg.depends_on or []), f"嵌套模块脚手架也须获序约束，实得 {reg.depends_on}"
    assert "st-1" not in (scaf.depends_on or [])


def test_rule4_writable_new_module_pom_counts_as_scaffold(tmp_path):
    """reviewer#3 整改：新模块 pom 被 LLM 误标进 writable（repo 基线无此文件）也算脚手架。"""
    from swarm.brain.contract_utils import normalize_plan_scopes

    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    st_reg = _st("st-1", writable=["pom.xml"])
    st_scaf = _st("st-6", writable=["alarm-interface/pom.xml"],
                  create=["alarm-interface/src/A.java"])
    plan = TaskPlan(subtasks=[st_reg, st_scaf])
    normalize_plan_scopes(plan, project_path=proj)
    reg = next(s for s in plan.subtasks if s.id == "st-1")
    assert "st-6" in (reg.depends_on or []), (
        f"writable 新模块 pom(基线无)也应判脚手架获序约束，实得 {reg.depends_on}"
    )


def test_scaffold_locator_suffix_and_case():
    """猎人#2 整改：worker 报相对 cwd 的模块目录（后缀）/大小写差异也能定位脚手架。"""
    from swarm.brain.nodes.recovery import _scaffold_subtask_of_module

    plan = TaskPlan(subtasks=[_st("st-6", create=["backend/crates/util/Cargo.toml"])])
    assert getattr(_scaffold_subtask_of_module(plan, "crates/util"), "id", None) == "st-6"
    assert getattr(_scaffold_subtask_of_module(plan, "Crates/Util"), "id", None) == "st-6"
    assert _scaffold_subtask_of_module(plan, "no-such") is None


# ═════════════════ (b) failure.py 定点重排阶梯 ═════════════════

def _wo(sid, l1_passed, details=None):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if l1_passed else "",
        summary="",
        l1_passed=l1_passed,
        l1_details=details or {},
        confidence="high" if l1_passed else "low",
    )


def _order_violation_state(targeted_recovery_count=0):
    st_reg = _st("st-reg", writable=["pom.xml"])
    st_scaf = _st("st-scaf", create=["M/pom.xml"])
    st_dl = _st("st-dl", create=["M/src/main/java/D.java"])
    plan = TaskPlan(subtasks=[st_reg, st_scaf, st_dl])
    return {
        "plan": plan,
        "failed_subtask_ids": ["st-dl"],
        "subtask_results": {
            "st-reg": _wo("st-reg", True),
            "st-scaf": _wo("st-scaf", True),
            "st-dl": _wo("st-dl", False, {
                "pipeline_blocked": "module_registered_before_scaffold",
                "blocked_on_modules": ["M"],
                "build_output": "[FATAL] Child module /workspace/M/pom.xml of "
                                "/workspace/pom.xml does not exist",
            }),
        },
        "subtask_retry_counts": {"st-dl": 3},
        "dispatch_remaining": [],
        "targeted_recovery_count": targeted_recovery_count,
    }


def _run_handle_failure(state, strategy="replan"):
    from swarm.brain.nodes import handle_failure

    async def _fake_invoke(self, msgs):
        class R:
            content = '{"strategy": "%s", "reasoning": "x"}' % strategy
        return R()

    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        inst = mock_llm.return_value
        inst.ainvoke = _fake_invoke.__get__(inst)
        return asyncio.run(handle_failure(state))


def test_order_violation_targeted_reorder():
    """撞序症状 → 插规范边(reg 依赖 scaf)、只重派失败、保兄弟、strategy=retry 系。"""
    state = _order_violation_state()
    result = _run_handle_failure(state)

    assert result["failure_strategy"] in ("retry", "retry_alternate"), result["failure_strategy"]
    assert result.get("failure_escalated") is not True
    # 规范边方向正确（reg after scaf），且非反向
    plan = result.get("plan") or state["plan"]
    reg = next(s for s in plan.subtasks if s.id == "st-reg")
    scaf = next(s for s in plan.subtasks if s.id == "st-scaf")
    assert "st-scaf" in (reg.depends_on or []), f"应插 reg→scaf 规范边，实际 {reg.depends_on}"
    assert "st-reg" not in (scaf.depends_on or []), "绝不能插反向边"
    # 成功兄弟未清、失败者重入派发
    assert "st-reg" in result["subtask_results"]
    assert "st-scaf" in result["subtask_results"]
    assert "st-dl" not in result["subtask_results"]
    assert "st-dl" in result["dispatch_remaining"]
    # reviewer#6 整改：失败子任务本身也补「等脚手架」边（scaffold 未完成时重派不再立即复撞）
    st_dl = next(s for s in plan.subtasks if s.id == "st-dl")
    assert "st-scaf" in (st_dl.depends_on or []), f"失败者应等脚手架，实得 {st_dl.depends_on}"


def test_order_violation_breaker_falls_through():
    """targeted_recovery_count 达上限 → 不再 mutate plan，落常规兜底（不无限循环）。"""
    from swarm.config.settings import get_config

    cap = get_config().model.max_retries
    state = _order_violation_state(targeted_recovery_count=cap)
    result = _run_handle_failure(state)
    # 达上限后不该再走定点重排（要么常规守卫 retry 要么 escalate，但绝不再自增 targeted_recovery_count）
    assert result.get("targeted_recovery_count", cap) <= cap + 1
    plan = result.get("plan") or state["plan"]
    reg = next(s for s in plan.subtasks if s.id == "st-reg")
    # 上限后不 mutate plan（防兜底路径留孤儿边）
    assert "st-scaf" not in (reg.depends_on or []), "达断路器上限后不得再改 plan"


def test_order_violation_cycle_guard_skips_insert():
    """scaf 已(传递)依赖 reg（除直接反边外的间接路径）→ 跳过插边（防环），落常规路径。"""
    state = _order_violation_state()
    plan = state["plan"]
    st_mid = _st("st-mid", writable=["x.java"], depends=["st-reg"])
    plan.subtasks.append(st_mid)
    scaf = next(s for s in plan.subtasks if s.id == "st-scaf")
    scaf.depends_on = ["st-mid"]  # scaf→mid→reg 间接依赖 reg
    result = _run_handle_failure(state)
    reg = next(s for s in (result.get("plan") or plan).subtasks if s.id == "st-reg")
    assert "st-scaf" not in (reg.depends_on or []), "间接反向依赖存在时插边=成环，必须跳过"
