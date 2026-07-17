"""R65TR-T2：失败第二次机会失真轴治本。

治后回放（963d78da）死因主链实证：st-2（trivial 根实体）漏写 plan 明示的
selectAlarmAppById，五次派发判死依据逐字相同、三次 diff 逐字节等长——
① trivial 快速路径确定性闸门判死后零修复迭代（medium/complex 有"修复尝试 1/3"）；
② retry_guidance 只装 LLM 诊断——brain 离线时 LLM 异常→零信息重试；在线时
   确定性判死依据也从不注入；
③ failure 侧日志宣称"换备选模型"，dispatch E1 判据判无异构备选→实派同模型+
   加步数，宣称与实派永久不符（L6454/6458 实锤）。

治本：
- W2 evaluate_l1 失败即 stamp details["det_fail_reason"]（单一仲裁器=单一 stamp
  点，覆盖 trivial/Phase3/Phase4；通过时清除，保持"存在⟺判死"不变量）；
- B1 handle_failure 确定性装填：无论 LLM 诊断有无，失败子任务的 det_fail_reason
  恒并入 retry_guidance（离线可用）；
- W1 trivial 判死后携判死原文一轮有界修复（重跑闸门；拒答即止）；
- B2 换备宣称接 router 真相（无异构备选时诚实说同模型+加步数）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: F401
from types import SimpleNamespace
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.types import (
    Complexity, FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan, WorkerOutput,
)
from swarm.worker.l1_verdict import evaluate_l1


# ── W2：仲裁器 stamp ──────────────────────────────────────────────────


def test_evaluate_l1_stamps_det_fail_reason_on_fail():
    v = evaluate_l1(
        det_ok=False,
        det_details={"verify_failed": "grep -q 'selectAlarmAppById' m/AlarmAppMapper.java"},
        verify_result=None, llm_ok=True, prior=None, phase="trivial",
    )
    assert not v.passed
    reason = v.details.get("det_fail_reason")
    assert reason and reason.startswith("verify_failed"), \
        f"判死 verdict 必带机读 det_fail_reason: {v.details}"
    assert "selectAlarmAppById" in reason


def test_evaluate_l1_no_stamp_on_pass():
    v = evaluate_l1(
        det_ok=True, det_details={"det_fail_reason": "stale-from-prev-attempt"},
        verify_result=None, llm_ok=True, prior=None, phase="trivial",
    )
    assert v.passed
    assert "det_fail_reason" not in v.details, \
        "通过 verdict 必须清除陈旧判死依据（存在⟺判死 不变量）"


# ── B1：handle_failure 确定性装填 ─────────────────────────────────────


def _plan():
    return TaskPlan(subtasks=[
        SubTask(id="st-1", description="脚手架", difficulty=SubTaskDifficulty.MEDIUM,
                modality=SubTaskModality.TEXT, scope=FileScope(create_files=["m/pom.xml"])),
        SubTask(id="st-2", description="AlarmApp 垂直切片", difficulty=SubTaskDifficulty.TRIVIAL,
                modality=SubTaskModality.TEXT,
                scope=FileScope(create_files=["m/src/AlarmAppMapper.java"]), depends_on=["st-1"]),
    ], parallel_groups=[["st-1"]])


def _state_with_failed_st2(det_reason: str | None):
    l1d = {"deterministic_gate": "verify"}
    if det_reason:
        l1d["det_fail_reason"] = det_reason
    return {
        "complexity": Complexity.ULTRA,
        "plan": _plan(),
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff="d", summary="ok", l1_passed=True),
            "st-2": WorkerOutput(subtask_id="st-2", diff="x", summary="verify 未过",
                                 l1_passed=False, l1_details=l1d),
        },
        "subtask_retry_counts": {"st-2": 0},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }


class _OfflineLLM:
    def __call__(self):
        raise RuntimeError("brain_offline_llm_blocked: 模拟离线")


def test_handle_failure_offline_still_injects_det_reason():
    """brain 离线（LLM 构造抛异常→确定性回退 retry）时，重试绝不能零信息：
    det_fail_reason 必须进 retry_guidance。回放实锤：st-2 五连跑判死原文从未抵达模型。"""
    det = "verify_failed: grep -q 'selectAlarmAppById' m/src/AlarmAppMapper.java"
    state = _state_with_failed_st2(det)
    with patch.object(nodes, "_get_brain_llm", _OfflineLLM()):
        out = asyncio.run(nodes.handle_failure(state))
    plan = out.get("plan")
    assert plan is not None, "retry_guidance 就地改必须随 plan 回传"
    st2 = next(s for s in plan.subtasks if s.id == "st-2")
    assert st2.retry_guidance and "selectAlarmAppById" in st2.retry_guidance, \
        f"离线回退重试必携确定性判死依据: {st2.retry_guidance!r}"


class _FakeResp:
    def __init__(self, content): self.content = content


def _fake_llm(reasoning: str, strategy="retry"):
    import json as _j
    payload = _j.dumps({"strategy": strategy, "reasoning": reasoning}, ensure_ascii=False)

    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp(payload)
    return lambda: _L()


def test_handle_failure_merges_llm_diagnosis_and_det_reason():
    det = "verify_failed: grep -q 'selectAlarmAppById' m/src/AlarmAppMapper.java"
    diag = "Mapper 方法命名须与验收字面一致，勿用 ByAppId 代替 ById"
    state = _state_with_failed_st2(det)
    with patch.object(nodes, "_get_brain_llm", _fake_llm(diag)):
        out = asyncio.run(nodes.handle_failure(state))
    st2 = next(s for s in out["plan"].subtasks if s.id == "st-2")
    rg = st2.retry_guidance or ""
    assert "ByAppId 代替 ById" in rg, f"LLM 诊断应保留: {rg!r}"
    assert "selectAlarmAppById" in rg and "判死依据" in rg, \
        f"确定性判死依据应并入: {rg!r}"


def test_handle_failure_success_sibling_untouched():
    state = _state_with_failed_st2("verify_failed: x")
    with patch.object(nodes, "_get_brain_llm", _OfflineLLM()):
        out = asyncio.run(nodes.handle_failure(state))
    st1 = next(s for s in out["plan"].subtasks if s.id == "st-1")
    assert not st1.retry_guidance


# ── W1：trivial 判死后一轮携依据修复 ──────────────────────────────────


def _mk_trivial_stub(gate_results: list, agent_log: list):
    """gate_results: 依次弹出的 (det_ok, det_details)；agent_log 收 (step, prompt)。"""
    from swarm.worker.executor import WorkerExecutor, WorkerPhase  # noqa: F401

    stub = SimpleNamespace()
    stub.subtask = SubTask(
        id="st-2", description="AlarmApp 垂直切片", difficulty=SubTaskDifficulty.TRIVIAL,
        scope=FileScope(create_files=["m/AlarmAppMapper.java"]))
    stub.phase = None
    stub._log = lambda m, level="info": None
    stub._scope_ops_hint = lambda: "【新建】m/AlarmAppMapper.java"
    stub._context_snippets_block = lambda: ""
    stub._trivial_alt_retried = False

    async def _run_agent(prompt, step=""):
        agent_log.append((step, prompt))
        return f"done:{step}"
    stub._run_agent = _run_agent

    async def _sync_from_sandbox(reason):
        return None
    stub._sync_from_sandbox = _sync_from_sandbox

    def _gate():
        return gate_results.pop(0)
    stub._deterministic_l1_gate = _gate
    stub._build_produce_prompt = lambda: "produce"

    def _parse(result, l1_passed, l1_details):
        return WorkerOutput(subtask_id="st-2", diff="d", summary="s",
                            l1_passed=l1_passed, l1_details=l1_details)
    stub._parse_produce_result = _parse
    stub._rollback_failed_manifest_footprint = lambda d: None
    stub._run_trivial_fast = WorkerExecutor._run_trivial_fast.__get__(stub)
    return stub


def test_trivial_repair_round_feeds_reason_and_regates():
    """首闸判死（verify_failed）→ 修复轮 agent 必须收到判死原文 → 复闸通过 → l1_passed。"""
    agent_log: list = []
    gates = [
        (False, {"verify_failed": "grep -q 'selectAlarmAppById' m/AlarmAppMapper.java"}),
        (True, {"deterministic_gate": "verify"}),
    ]
    stub = _mk_trivial_stub(gates, agent_log)
    out = asyncio.run(stub._run_trivial_fast())
    steps = [s for s, _ in agent_log]
    assert any("repair" in s or "fix" in s for s in steps), f"应有修复轮: {steps}"
    _repair_prompt = next(p for s, p in agent_log if "repair" in s or "fix" in s)
    assert "selectAlarmAppById" in _repair_prompt, "判死原文必须喂给修复轮"
    # 复核 MED 锁：修复轮必须带任务原意，防"字面满足式假修复"（塞空壳过 grep）
    assert "AlarmApp 垂直切片" in _repair_prompt, "修复轮必须携任务原意"
    assert out.l1_passed, "复闸通过应翻盘为通过"
    assert not gates, "两次闸门都应被消费（判死→修复→复闸）"


def test_trivial_repair_bounded_single_round():
    """修复轮有界：两闸皆死 → 只修一轮（不无限循环），终判失败。"""
    agent_log: list = []
    gates = [
        (False, {"verify_failed": "grep A"}),
        (False, {"verify_failed": "grep A"}),
    ]
    stub = _mk_trivial_stub(gates, agent_log)
    out = asyncio.run(stub._run_trivial_fast())
    repair_steps = [s for s, _ in agent_log if "repair" in s or "fix" in s]
    assert len(repair_steps) == 1, f"修复轮必须有界一轮: {agent_log}"
    assert not out.l1_passed
    assert (out.l1_details or {}).get("det_fail_reason"), "终判失败必带机读判死依据"


def test_trivial_no_repair_when_first_gate_passes():
    agent_log: list = []
    gates = [(True, {"deterministic_gate": "verify"})]
    stub = _mk_trivial_stub(gates, agent_log)
    out = asyncio.run(stub._run_trivial_fast())
    assert out.l1_passed
    assert not [s for s, _ in agent_log if "repair" in s or "fix" in s], "通过不修复"


def test_trivial_repair_kill_switch(monkeypatch):
    monkeypatch.setenv("SWARM_WORKER_TRIVIAL_REPAIR", "false")
    agent_log: list = []
    gates = [(False, {"verify_failed": "grep A"})]
    stub = _mk_trivial_stub(gates, agent_log)
    out = asyncio.run(stub._run_trivial_fast())
    assert not out.l1_passed
    assert not [s for s, _ in agent_log if "repair" in s or "fix" in s], "开关关闭不修复"


# ── 猎手整改锁 ────────────────────────────────────────────────────────


def test_trivial_repair_agent_exception_keeps_first_verdict():
    """猎手 HIGH：修复轮 agent 异常绝不冒泡——跳过修复，保首轮判死+产出+机读依据。"""
    agent_log: list = []
    gates = [(False, {"verify_failed": "grep -q 'selectAlarmAppById' m/AlarmAppMapper.java"})]
    stub = _mk_trivial_stub(gates, agent_log)
    _orig_run_agent = stub._run_agent

    async def _raising(prompt, step=""):
        if "repair" in step:
            raise TimeoutError("模型超时")
        return await _orig_run_agent(prompt, step=step)
    stub._run_agent = _raising

    out = asyncio.run(stub._run_trivial_fast())  # 不得抛
    assert not out.l1_passed
    assert (out.l1_details or {}).get("det_fail_reason"), \
        "异常跳修复后首轮机读判死依据必须保留（B1 装填的口粮）"
    assert any(s == "produce" for s, _ in agent_log), "produce 段必须照常执行（保产出）"


def test_trivial_repair_sync_exception_keeps_first_verdict():
    """猎手 HIGH：修复轮 pull-back 异常同样不冒泡，保首轮判决。"""
    agent_log: list = []
    gates = [(False, {"verify_failed": "grep A"})]
    stub = _mk_trivial_stub(gates, agent_log)
    _calls = {"n": 0}

    async def _sync(reason):
        _calls["n"] += 1
        if _calls["n"] >= 2:  # 首轮 pull-back 成功，修复轮 pull-back 炸
            raise RuntimeError("TransientInfraError: 沙箱不可达")
    stub._sync_from_sandbox = _sync

    out = asyncio.run(stub._run_trivial_fast())
    assert not out.l1_passed
    assert (out.l1_details or {}).get("det_fail_reason")
    assert any(s == "produce" for s, _ in agent_log)


def test_b1_offline_flicker_preserves_prior_llm_diagnosis():
    """猎手 MED：上轮在线诊断在本轮 LLM 闪断时不得被裸 det 依据覆盖倒退。"""
    det = "verify_failed: grep -q 'selectAlarmAppById' m/src/AlarmAppMapper.java"
    state = _state_with_failed_st2(det)
    st2_pre = next(s for s in state["plan"].subtasks if s.id == "st-2")
    st2_pre.retry_guidance = (
        "Mapper 方法命名须与验收字面一致，勿用 ByAppId 代替 ById\n"
        "上次尝试的确定性判死依据（机读，必须针对性修复后再交付）：verify_failed: 旧行")
    with patch.object(nodes, "_get_brain_llm", _OfflineLLM()):
        out = asyncio.run(nodes.handle_failure(state))
    st2 = next(s for s in out["plan"].subtasks if s.id == "st-2")
    rg = st2.retry_guidance or ""
    assert "ByAppId 代替 ById" in rg, f"上轮语义诊断不得丢失: {rg!r}"
    assert "selectAlarmAppById" in rg, f"本轮 det 依据应在: {rg!r}"
    assert "verify_failed: 旧行" not in rg, f"旧确定性依据行应被本轮替换（防跨轮堆叠）: {rg!r}"


def test_b2_judge_exception_falls_back_to_no_alternate(caplog):
    """猎手 MED：判据异常回退方向=保守【无备选】（与 _has_hetero_alternate 惯例一致），
    绝不谎称换备。"""
    import importlib
    import logging
    _dsp = importlib.import_module("swarm.brain.nodes.dispatch")

    def _boom(d):
        raise RuntimeError("router 异常")
    state = _state_with_failed_st2("verify_failed: x")
    state["subtask_retry_counts"] = {"st-2": 2}
    with patch.object(nodes, "_get_brain_llm", _OfflineLLM()), \
         patch.object(_dsp, "_has_hetero_alternate", _boom), \
         caplog.at_level(logging.INFO):
        asyncio.run(nodes.handle_failure(state))
    lines = [r.getMessage() for r in caplog.records if "retry_alternate" in r.getMessage()]
    assert lines and any("同模型" in ln for ln in lines), \
        f"判据异常必须保守宣称同模型: {lines}"


# ── B2：换备宣称接 router 真相 ────────────────────────────────────────


def test_alternate_claim_truthful_when_no_hetero_alternate(caplog):
    """强制 alternate 档但 router 无异构备选 → 日志不得谎称"换备选模型"，
    须诚实标注实派同模型+加步数。"""
    import importlib
    import logging
    _dsp = importlib.import_module("swarm.brain.nodes.dispatch")  # 包属性被同名函数遮蔽，须 importlib
    state = _state_with_failed_st2("verify_failed: x")
    state["subtask_retry_counts"] = {"st-2": 2}  # 恰越 max_retries(2) → forced_alternate 档
    with patch.object(nodes, "_get_brain_llm", _OfflineLLM()), \
         patch.object(_dsp, "_has_hetero_alternate", lambda d: False), \
         caplog.at_level(logging.INFO):
        asyncio.run(nodes.handle_failure(state))
    lines = [r.getMessage() for r in caplog.records
             if "retry_alternate" in r.getMessage()]
    assert lines, "应有 retry_alternate 策略日志"
    assert any("同模型" in ln for ln in lines), \
        f"无异构备选时必须诚实宣称实派同模型: {lines}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
