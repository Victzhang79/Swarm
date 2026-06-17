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


# ── P1-DEBT-07 根因回归（真行为测，复现 task 69d34b1b 假 DONE）──
# 漏网场景：DELIVER 阶段 REJECT（虚假前提阻断 / escalate），confirm_reason 为空、
# verification_failure 为空，且 l2_passed 被 BrainState last-write-wins 污染成 True。
# 旧逻辑：433 行 if 不命中 → 落 454 gates 复核 → gates 看污染的 l2_passed=True → 放行 → 假 DONE。
# 修复后：human_decision==REJECT 即判 FAILED（与图 after_deliver 路由同源），不看 l2_passed。
@pytest.mark.asyncio
async def test_debt07_deliver_reject_lands_failed_not_done(monkeypatch):
    import asyncio
    import swarm.brain.runner as r
    from swarm.types import HumanDecision

    captured = {}

    def _fake_update_task(task_id, **kw):
        if "status" in kw:
            captured["status"] = kw["status"]

    monkeypatch.setattr(r.store, "update_task", _fake_update_task)
    monkeypatch.setattr(r.store, "get_task", lambda tid: {"project_id": "p1", "description": "x"})
    monkeypatch.setattr(r.store, "estimate_token_usage", lambda **k: 0)
    monkeypatch.setattr(r.store, "compute_task_duration_seconds", lambda rec: 0.0)
    monkeypatch.setattr(r, "_emit_task_notification", lambda *a, **k: None)
    monkeypatch.setattr(r, "_sync_task_from_state", lambda *a, **k: None)
    monkeypatch.setattr(r, "_extract_interrupt_info", lambda *a, **k: None)

    async def _fake_emit(*a, **k):
        return None
    monkeypatch.setattr(r, "_emit", _fake_emit)

    # 精确复现 69d34b1b：REJECT + l2_passed 被污染为 True + 无 confirm_reason/verification_failure
    polluted_state = {
        "task_id": "t-debt07",
        "auto_accept": True,
        "human_decision": HumanDecision.REJECT,
        "deliver_auto_reject_reason": "l2_failed: L2 集成验证未通过",
        "l2_passed": True,  # ← 污染：若仍依赖此字段会误判 DONE
    }
    await r._handle_post_run("t-debt07", polluted_state, asyncio.Queue(), None)
    assert captured.get("status") == "FAILED", (
        f"DELIVER 阶段 REJECT 必须落 FAILED（与图路由同源），实际={captured.get('status')}；"
        "若为 DONE 说明又退回依赖可被污染的 l2_passed（P1-DEBT-07 回归）"
    )


# ── P1-DEBT-12 STAGE2 并行 + 双护栏：信号量 + 单模块超时 + 失败硬告警聚合 ──
def test_debt12_stage2_parallel_with_guards():
    import swarm.brain.planning_nodes as pn
    src = inspect.getsource(pn)
    assert "gather" in src, "STAGE2 应 asyncio.gather 并行各模块"
    assert "Semaphore" in src and "_STAGE2_CONCURRENCY" in src, "STAGE2 应有信号量限并发（单 key 友好）"
    assert "wait_for" in src and "_STAGE2_MODULE_TIMEOUT" in src, "STAGE2 应有单模块超时（防 hang）"
    assert "failed_modules" in src and "stage2_failed_modules" in src, "失败模块应聚合并回传供下游对账"


@pytest.mark.asyncio
async def test_debt12_stage2_runs_concurrently(monkeypatch):
    """并行行为测：4 模块各 sleep 0.3s，并发=3 → 总耗时应 < 串行(1.2s)，约 2 波≈0.6s。"""
    import asyncio
    import json
    import time
    import swarm.brain.planning_nodes as pn

    # stage1 返回 4 模块；stage2 每次 sleep 0.3s 模拟 LLM 延迟
    stage1_content = json.dumps({
        "modules": [{"name": f"mod{i}", "responsibility": "r", "est_files": 2} for i in range(4)],
        "architecture": "arch", "data_model": "dm", "shared_contract": {},
    })
    stage2_content = json.dumps({"file_plan": [{"path": "x.py", "action": "create"}]})

    class _Resp:
        def __init__(self, content):
            self.content = content

    class _LLM:
        _calls = 0
        async def ainvoke(self, msgs, *a, **k):
            _LLM._calls += 1
            if _LLM._calls == 1:
                return _Resp(stage1_content)  # stage1：立即返回模块清单
            await asyncio.sleep(0.3)          # stage2：模拟延迟
            return _Resp(stage2_content)

    # 隔离 stage1 对 state 的依赖
    monkeypatch.setattr(pn, "_format_knowledge", lambda state: "")

    llm = _LLM()
    t0 = time.monotonic()
    result, fp, _fi, _c = await pn._tech_design_staged(
        llm, "task desc", "ultra", False, {},
        "facts", "", "",
    )
    elapsed = time.monotonic() - t0

    assert len(result["modules"]) == 4
    assert len(fp) == 4, "4 模块各产 1 文件，应聚合 4 个"
    # 并发=3：4 模块分 2 波（3+1），每波 0.3s → ~0.6s；串行需 1.2s。给足余量断 < 1.0s
    assert elapsed < 1.0, f"STAGE2 应并行（实测 {elapsed:.2f}s，串行需 ~1.2s）"


# ── N-12 检索崩溃可感知 ──
def test_n12_analyze_warns_on_retrieval_error():
    src = inspect.getsource(__import__("swarm.brain.nodes", fromlist=["analyze"]).analyze)
    assert 'stats.get("error")' in src, "analyze 应检查 stats.error 区分检索崩溃 vs 无知识"
