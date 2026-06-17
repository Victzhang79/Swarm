"""SWARM_CTO_GUIDE 影响 ultra 主流程正确性的活 bug 回归（N-02/03/04/05/12 + P1-DEBT-07）。

多数是"读无写键/键漂移/静默误判"，用源码静态断言守护键名一致性（避免重量级 e2e）。
N-05 用真实行为测（最危险：失败判通过造假 DONE）。
"""
import inspect

import pytest


# ── N-05 L2 坏 JSON all([])→True（行为测，最危险）──
@pytest.mark.asyncio
async def test_n05_l2_empty_results_not_pass(monkeypatch):
    """L2 LLM 坏 JSON 回退时，无 WorkerOutput 佐证必须判失败（防 all([])→True 假 DONE）。"""
    import json as _json
    import swarm.brain.nodes as nodes_mod

    class _Resp:
        content = "这不是合法JSON{{{"

    class _LLM:
        async def ainvoke(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(nodes_mod, "_get_brain_llm", lambda: _LLM())
    # 空 subtask_results → 必须 False（不能 all([])→True）
    r = await nodes_mod._verify_l2_via_llm("task", "diff", [], {})
    assert r is False, "空结果集回退必须判失败，不能 all([])→True 造假 DONE"


# ── N-02 REVISION 从 revision_subtasks[0] 读 ──
def test_n02_revision_reads_nested_array():
    src = inspect.getsource(__import__("swarm.brain.nodes", fromlist=["revision"]).revision)
    assert "revision_subtasks" in src, "REVISION 应从 revision_subtasks[0] 取（非顶层）"


# ── N-03 ultra 分批 prompt 用 acceptance_criteria 键 ──
def test_n03_batch_prompt_uses_acceptance_criteria():
    from swarm.brain.prompts import PLAN_BATCH_SYSTEM
    assert "acceptance_criteria" in PLAN_BATCH_SYSTEM, "分批 prompt 键应对齐 SubTask.acceptance_criteria"
    # 不应再用会被 extra=ignore 丢弃的旧键 acceptance（裸）
    assert '"acceptance"' not in PLAN_BATCH_SYSTEM, "不应用旧键 acceptance（静默丢弃）"


# ── N-04 verify_l3 返回 l3_branch ──
def test_n04_verify_l3_returns_branch():
    import swarm.brain.nodes.verify as v
    src = inspect.getsource(v.verify_l3) if hasattr(v, "verify_l3") else inspect.getsource(v)
    assert '"l3_branch"' in src, "verify_l3 应返回 l3_branch（否则 MR 指向未推送分支）"


def test_n04_state_has_l3_branch_field():
    from swarm.brain.state import BrainState
    assert "l3_branch" in BrainState.__annotations__, "BrainState 应有 l3_branch 字段"


# ── P1-DEBT-07 runner 终态下沉 gates ──
def test_debt07_runner_uses_gates_for_terminal():
    import swarm.brain.runner as r
    src = inspect.getsource(r)
    assert "can_auto_accept_delivery" in src, "runner 终态判定应复用 gates.can_auto_accept_delivery"


def test_debt07_gates_rejects_escalated():
    from swarm.brain import gates
    allow, reason = gates.can_auto_accept_delivery({"failure_escalated": True})
    assert allow is False and "escalat" in reason.lower()


# ── N-12 检索崩溃可感知 ──
def test_n12_analyze_warns_on_retrieval_error():
    src = inspect.getsource(__import__("swarm.brain.nodes", fromlist=["analyze"]).analyze)
    assert 'stats.get("error")' in src, "analyze 应检查 stats.error 区分检索崩溃 vs 无知识"
