"""阶段6 批2（登记册 §五）：契约有牙——D5/D10/D13/D14/D15/C4 行为锁。

D5 契约检查二态皆坏：全缺才 fail（缺 90% 也放行=形同虚设）→ 缺失率阈值
   （SWARM_CONTRACT_MISSING_RATIO 默认 0.4）+ issue 带缺失清单；verify 侧按缺失符号
   归因 owner 定向重派（归因不出回退全员，绝不漏修）。
D10 契约 Stage C 裸 name 全局自并：跨模块同名接口强行合体（module 归属取首版，
   round37 实测 168→148 来源）→ 合并键 (module, name)。
D13 契约失败复用 subtask_retry_counts=交叉挤兑个体 capability 配额 → 独立
   contract_retry_counts（登记+D08 签名剪枝）。
D14 dedupe_module_scaffolds 只 union 3 scope 字段——dup 的 acceptance_criteria/
   delete_files/create_dirs/description 静默丢弃 → 补 union。
D15 fix_dependency_ordering 规则2 无条件清空脚手架 depends_on——父 pom 先于子模块
   清单的合法上游序被抹平 → 只剥非脚手架依赖（scaffold→scaffold 保留）。
C4 非空 diff 但无 test/verify 命令=语义正确性零覆盖 → needs_review 标记可观测。
"""

from __future__ import annotations

from swarm.brain.integration_review import check_contract_in_diff

# ─────────────── D5：缺失率阈值 ───────────────


def _contract(n):
    return {"interfaces": [f"Iface{i:02d}Service" for i in range(n)]}


def test_d5_partial_missing_over_threshold_fails(monkeypatch):
    monkeypatch.setenv("SWARM_CONTRACT_MISSING_RATIO", "0.4")
    sc = _contract(10)
    diff = "+class Iface00Service {}\n+class Iface01Service {}\n"  # 8/10 缺失
    ok, issues = check_contract_in_diff(diff, sc)
    assert ok is False, "旧判定全缺才 fail——缺 80% 也放行=契约检查形同虚设"
    assert issues and "缺失清单" in issues[0], "issue 必须带逐符号明细（verify 侧归因 owner 用）"


def test_d5_small_gap_within_threshold_passes(monkeypatch):
    monkeypatch.setenv("SWARM_CONTRACT_MISSING_RATIO", "0.4")
    sc = _contract(10)
    diff = "\n".join(f"+class Iface{i:02d}Service {{}}" for i in range(8))  # 2/10 缺失
    ok, _ = check_contract_in_diff(diff, sc)
    assert ok is True, "小缺口≤阈值放行（防误杀，残差由验收断言兜底）"


def test_d5_all_present_passes():
    sc = _contract(3)
    diff = "\n".join(f"+class Iface{i:02d}Service {{}}" for i in range(3))
    assert check_contract_in_diff(diff, sc) == (True, [])


# ─────────────── D10：(module,name) 合并键 ───────────────

def test_d10_cross_module_same_name_not_merged():
    from swarm.brain.planning_nodes import _merge_module_contracts
    skeleton = {"interfaces": [], "dtos": [], "constants": []}
    slices = [
        {"interfaces": [{"name": "IUserService", "module": "mod-a",
                         "signature": ["a()"]}]},
        {"interfaces": [{"name": "IUserService", "module": "mod-b",
                         "signature": ["b()"]}]},
    ]
    merged = _merge_module_contracts(skeleton, slices)
    ifaces = merged.get("interfaces") or []
    assert len(ifaces) == 2, (
        "跨模块同名接口=不同契约——裸 name 键强行合体（module 取首版）会造"
        "接口爆炸自并（round37 实测 168→148）")
    _mods = {i.get("module") for i in ifaces}
    assert _mods == {"mod-a", "mod-b"}


def test_d10_same_module_still_unions():
    from swarm.brain.planning_nodes import _merge_module_contracts
    skeleton = {"interfaces": [], "dtos": [], "constants": []}
    slices = [
        {"interfaces": [{"name": "ISvc", "module": "m", "signature": ["a()"]}]},
        {"interfaces": [{"name": "ISvc", "module": "m", "signature": ["b()"]}]},
    ]
    merged = _merge_module_contracts(skeleton, slices)
    ifaces = [i for i in (merged.get("interfaces") or []) if i.get("name") == "ISvc"]
    assert len(ifaces) == 1, "同模块同名（bisect/边界重叠）照旧并集保方法"
    assert set(ifaces[0].get("signature") or []) == {"a()", "b()"}


# ─────────────── D13：独立契约重试表 ───────────────

def test_d13_registry_and_state():
    import typing

    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState
    hints = typing.get_type_hints(BrainState, include_extras=True)
    assert "contract_retry_counts" in hints
    assert ACCOUNTING_KEY_LIFECYCLE.get("contract_retry_counts") == "monotonic", (
        "契约是横切集成面失败——复用 capability 配额表=交叉挤兑个体重试额度")


def test_d13_contract_branch_uses_independent_table():
    import asyncio
    from unittest.mock import patch

    import swarm.brain.nodes as nodes
    from swarm.types import Confidence, FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="a", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["a.py"], readable=[]))],
        parallel_groups=[["st-1"]])
    state = {
        "complexity": "complex", "plan": plan,
        "verification_failure": "contract",
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": WorkerOutput(
            subtask_id="st-1", diff="+x\n", summary="", l1_passed=True,
            confidence=Confidence.HIGH)},
        "dispatch_remaining": [],
        "subtask_retry_counts": {"st-1": 2},  # capability 配额已 2 次
    }
    with patch.object(nodes, "_get_brain_llm", side_effect=RuntimeError("no llm")):
        out = asyncio.run(nodes.handle_failure(state))
    assert out.get("contract_retry_counts", {}).get("st-1") == 1, "契约重试记独立表"
    assert "subtask_retry_counts" not in out or \
        out["subtask_retry_counts"].get("st-1", 2) == 2, (
        "capability 配额不被契约失败消耗（否则 2+1=3 直接触顶 escalate）")
    assert out.get("failure_strategy") in ("retry", "escalate")
    assert out.get("failure_escalated") is not True, "独立表首轮绝不触顶"


# ─────────────── D14/D15 ───────────────

def _scaffold(sid, pom, deps=None, ac=None):
    from swarm.types import FileScope, SubTask, SubTaskDifficulty
    return SubTask(id=sid, description=f"建 {pom}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=[], readable=[], create_files=[pom]),
                   depends_on=deps or [], acceptance_criteria=ac or [])


def test_d14_dedupe_unions_acceptance_criteria():
    from swarm.brain.contract_utils import dedupe_module_scaffolds
    from swarm.types import TaskPlan
    a = _scaffold("st-1", "mod/pom.xml", ac=["构建通过"])
    b = _scaffold("st-2", "mod/pom.xml", ac=["含 alarm 依赖"])
    plan = TaskPlan(subtasks=[a, b], parallel_groups=[["st-1", "st-2"]])
    dedupe_module_scaffolds(plan)
    kept = plan.subtasks[0]
    assert set(kept.acceptance_criteria or []) >= {"构建通过", "含 alarm 依赖"}, (
        "dup 独有的验收标准静默丢弃=验收面缩水")


def test_d15_scaffold_to_scaffold_dependency_preserved():
    from swarm.brain.contract_utils import fix_dependency_ordering
    from swarm.types import TaskPlan
    root = _scaffold("st-root", "pom.xml")
    child = _scaffold("st-child", "mod/pom.xml", deps=["st-root"])
    plan = TaskPlan(subtasks=[root, child], parallel_groups=[["st-root"], ["st-child"]])
    fix_dependency_ordering(plan)
    child2 = next(s for s in plan.subtasks if s.id == "st-child")
    assert "st-root" in (child2.depends_on or []), (
        "父 pom 先于子模块清单是合法上游序——无条件置根会让 greenfield 并行建清单"
        "撞 reactor 时序错误且无回补")


# ─────────────── C4：needs-review 标记 ───────────────

def test_c4_no_verify_commands_marks_needs_review(tmp_path):
    from swarm.types import FileScope, SubTask, SubTaskDifficulty
    from swarm.worker.l1_pipeline import run_l1_pipeline
    (tmp_path / "a.py").write_text("x = 1\n")
    st = SubTask(id="st-c4", description="改 a.py", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.py"], readable=[]), intent="modify")
    diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    ok, details = run_l1_pipeline(str(tmp_path), st, diff, llm=None)
    assert details.get("needs_review") == "no_test_or_verify_commands", (
        "非空 diff 但语义正确性零覆盖（test skip 判过+自检 advisory）——必须可观测")
