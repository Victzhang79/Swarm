"""阶段0 对抗双复核治理批（2026-07-09）— reviewer + silent-failure-hunter 发现全治。

R1 [CONFIRMED reviewer]：A4 让 gates 的 plan_batch_failed 专属归因分支不可达——
   plan_valid=False 先被通用 plan_invalid 分支拦截，重试耗尽后 _vf 误标 plan_invalid、
   丢 failure_escalated（round29 专门建的归因被架空，污染 L5 错题）。
   修：gates 里 plan_batch_failed 判定先于 plan_valid 通用判定。
R2 [CONFIRMED 双方]：A1×A4 组合——ULTRA 分批部分失败（_plan_batch_failed 非空）时
   _plan_degraded 仍 None → A1 照清 replan_feedback → A4 打回的补齐重试轮里，失败模块
   重拆 prompt 丢失最初执行失败根因教训。修：清空条件加 `and not _plan_batch_failed`。
H1 [CONFIRMED hunter]：_previous_plan_repair_block 的 baseline 摘要 [:1200] 截断无自述，
   而提示要求"保留申报"——A8 帽升 500 后触发面变大，未展示申报会被 LLM 丢弃。
   修：截断自述（总条数+未列出同样有效）。
H2 [PLAUSIBLE hunter]：裸 TimeoutError/asyncio.TimeoutError 的 str() 为空，classify_failure
   文本特征全绕过 → F12 重试对最典型的超时形态失明。修：classify_failure isinstance 步
   纳入 TimeoutError（修一类：failure.py/executor.py 同受益）。
H3 [PLAUSIBLE hunter]：A2 豁免仅按 basename——PRD 点名路径形态 com/b/X.java（真缺失）
   会被 file_plan 计划新建的 com/a/X.java 跨目录碰撞误豁免。修：路径形态按后缀匹配，
   裸文件名保持 basename 口径。
H4 [PLAUSIBLE hunter]："稍后重试/服务繁忙"是叙述性客套话，free-form summary 兜底分类
   会把确定性 capability 失败误判 transient（有界 3 次但纯浪费）。修：从 marker 表剔除
   这两个叙述短语（连接中断/限流/超时等具体特征保留）。
"""

from __future__ import annotations

import asyncio

from swarm.brain.gates import can_auto_accept_plan
from swarm.brain.nodes import _previous_plan_repair_block, confirm_plan, plan
from swarm.brain.planning_nodes import _label_grounded_fact_issues
from swarm.models.errors import TRANSIENT, classify_failure
from swarm.types import Complexity, FileScope, SubTask, SubTaskDifficulty, TaskPlan

_FAILED = [{"name": "mod-broken", "files": 3, "reason": "timeout"}]


def _plan_obj():
    return TaskPlan(
        subtasks=[SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=["a"]))],
        parallel_groups=[["st-1"]],
    )


# ─────────────────── R1：plan_batch_failed 归因先于 plan_invalid ───────────────────

def test_gates_plan_batch_failed_attribution_survives_invalid_plan():
    """重试耗尽后 plan_valid=False + 失败模块并存 → 归因必须是 plan_batch_failed
    （round29 专属分类），不得被通用 plan_invalid 遮蔽。"""
    allow, reason = can_auto_accept_plan({
        "plan_valid": False,
        "plan_validation_issues": ["整模块分解失败(timeout): mod-broken（3 文件）"],
        "plan_batch_failed_modules": list(_FAILED),
    })
    assert not allow
    assert reason.startswith("plan_batch_failed"), (
        f"归因被 plan_invalid 遮蔽（L5 错题污染，round29 归因被 A4 架空）: {reason}")


def test_confirm_reject_escalates_plan_batch_failed_after_retry_exhaustion():
    """生产路径终局：validate 打回耗尽 → plan_valid=False 进 confirm →
    _vf=plan_batch_failed + failure_escalated（非 plan_invalid 裸 REJECT）。"""
    out = confirm_plan({
        "plan": _plan_obj(),
        "plan_valid": False,
        "plan_validation_issues": ["整模块分解失败(timeout): mod-broken（3 文件）"],
        "complexity": Complexity.ULTRA,
        "auto_accept": True,
        "plan_batch_failed_modules": list(_FAILED),
    })
    assert out["verification_failure"] == "plan_batch_failed"
    assert out.get("failure_escalated") is True


# ─────────────────── R2：分批部分失败不清 replan_feedback ───────────────────

async def test_partial_batch_failure_preserves_replan_feedback(monkeypatch):
    """ULTRA 分批部分失败（_plan_degraded=None）→ replan_feedback 必须保留——
    否则 A4 打回的补齐重试轮里，失败模块重拆丢最初执行失败根因（F-3 被跨提交击穿）。"""
    import swarm.brain.nodes as nodes

    async def _fake_batched(llm, state, desc, kc, sliding_ctx, file_plan):
        return _plan_obj(), list(_FAILED), [], {}

    monkeypatch.setattr(nodes, "_plan_ultra_batched", _fake_batched)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: None)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    fb = "上轮执行失败根因：依赖悬空"
    out = await plan({
        "task_description": "big task",
        "complexity": Complexity.ULTRA,
        "tech_design_file_plan": [{"path": f"m/f{i}.txt", "action": "create"}
                                  for i in range(40)],
        "replan_feedback": fb,
        "replan_count": 1,
    })
    assert out["plan_batch_failed_modules"] == _FAILED  # 前提成立：部分失败轮
    assert (out.get("replan_feedback") or "") == fb, (
        "部分失败轮清空 replan_feedback=失败模块补齐重拆时丢执行失败教训")


# ─────────────────── H1：baseline 摘要截断自述 ───────────────────

def test_repair_block_baseline_truncation_self_describes():
    prev = _plan_obj()
    baseline = [{"id": f"req-{i:08x}", "reason": "存量模块已完整实现该能力点" * 2}
                for i in range(60)]  # 远超 1200 字符
    block = _previous_plan_repair_block(prev, baseline)
    assert "申报摘要已截断" in block and "60" in block, (
        "截断必须自述总条数——否则'保留申报'指令反向诱导 LLM 丢未展示条目")


def test_repair_block_short_baseline_no_truncation_note():
    block = _previous_plan_repair_block(
        _plan_obj(), [{"id": "req-1", "reason": "r"}])
    assert "申报摘要已截断" not in block


# ─────────────────── H2：TimeoutError isinstance 纳入 transient ───────────────────

def test_bare_timeout_error_classifies_transient():
    """str(TimeoutError()) == '' 绕过全部文本特征——isinstance 步必须兜住。"""
    assert classify_failure(asyncio.TimeoutError()) == TRANSIENT
    assert classify_failure(TimeoutError()) == TRANSIENT


def test_vision_retries_bare_timeout(monkeypatch):
    import swarm.brain.vision_ingest as vi
    import swarm.models.router as router_mod

    class _L:
        calls = 0

        async def ainvoke(self, msgs):
            _L.calls += 1
            if _L.calls == 1:
                raise asyncio.TimeoutError()

            class _R:
                content = "ok"
            return _R()

    class _FR:
        def get_model_by_name(self, n, temperature=0.2):
            return _L()

    monkeypatch.setattr(router_mod, "ModelRouter", lambda: _FR())
    monkeypatch.setattr(vi, "_RETRY_BACKOFF_BASE", 0.01)
    out = asyncio.run(vi._ainvoke_vision("m", ["data:image/png;base64,AA"]))
    assert out == "ok" and _L.calls == 2


# ─────────────────── H3：A2 路径形态按后缀匹配 ───────────────────

def _check(file, exists=False):
    return {"file": file, "exists": exists, "confidence": "high",
            "sources": [], "candidates": []}


def test_path_formed_claim_not_exempted_by_cross_dir_collision():
    """PRD 点名 com/b/Service.x（真缺失）+ file_plan 计划新建 com/a/Service.x →
    跨目录同名不得误豁免（round37 同名接口爆炸是本仓已证实模式）。"""
    issues = _label_grounded_fact_issues(
        [], [_check("com/b/Service.x")],
        [{"path": "com/a/Service.x", "action": "create"}])
    assert any(i.get("grounded") for i in issues), "跨目录 basename 碰撞被误豁免"


def test_path_formed_claim_exempted_on_suffix_match():
    issues = _label_grounded_fact_issues(
        [], [_check("b/Service.x")],
        [{"path": "src/com/b/Service.x", "action": "create"}])
    assert all(not i.get("grounded") for i in issues)


def test_bare_name_claim_keeps_basename_exemption():
    issues = _label_grounded_fact_issues(
        [], [_check("Service.x")],
        [{"path": "com/a/Service.x", "action": "create"}])
    assert all(not i.get("grounded") for i in issues)


# ─────────────────── H4：叙述性客套话不判 transient ───────────────────

def test_narrative_politeness_not_transient():
    assert classify_failure("模型服务繁忙") is None
    assert classify_failure("操作失败，请稍后重试") is None


def test_specific_cjk_markers_still_transient():
    assert classify_failure("连接中断，请稍后重试") == TRANSIENT  # 具体特征仍在
    assert classify_failure("触发限流") == TRANSIENT
