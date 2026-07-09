"""T4 — 运行时冒烟无进展 plateau 检测（ECC §D "plateau" 半环）测试。

取证坐实：swarm 的运行时冒烟已满足 ECC §D 的三分之：INCONCLUSIVE 绝不静默判 pass
（classify_smoke_outcome 唯一 passed 出口 = 探活 ok ∧ 进程存活）、三态 None≠False 全接线、
失败降级 fail-closed 派生。**max-iteration 熔断也在**（replan_count 永不重置、达 max_retries
即 escalate；targeted_recovery_counts 按子任务配额）。唯一缺口 = ECC §D "plateau" 半环：
既有轮次计数只按【次数】封顶，从不比对【连续两轮是否同一失败形态】——一个反复以完全相同
classification + 归因子任务集失败的冒烟，会白烧满 replan 预算才停（token 黑洞）。

T4 补 plateau 半环：handle_failure 运行时分支跨轮比对失败签名
（classification|归因子任务集排序）。
- 默认【仅观测留痕】：控制流不变（既有轮次计数兜底），绝不误伤"隐性收敛中"的修复；
- strict opt-in（SWARM_RUNTIME_SMOKE_PLATEAU_STRICT=1）：连续同签名→短路提前 escalate，省无谓重试。
镜像 T3 的观测/strict 二态哲学（绝不误伤 + 通用多栈：只读退出码语义与文件路径，栈无关）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan, WorkerOutput


def _st(sid, *, writable):
    return SubTask(
        id=sid,
        description="d",
        scope=FileScope(writable=writable),
        harness=TaskHarness(language="python"),
    )


def _wo(sid, l1_passed=True):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if l1_passed else "",
        summary="",
        l1_passed=l1_passed,
        l1_details={},
        confidence="high",
    )


def _run(state):
    from swarm.brain.nodes import handle_failure

    async def _fake_invoke(self, msgs):
        class R:
            content = '{"strategy": "replan", "reasoning": "x"}'

        return R()

    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        inst = mock_llm.return_value
        inst.ainvoke = _fake_invoke.__get__(inst)
        return asyncio.run(handle_failure(state))


def _unattributed_state(*, classification="code_error", last_signature="", replan_count=0):
    """归因不出（log 无匹配写者子任务）→ 走 replan 阶梯；签名 = 'classification|'。"""
    return {
        "verification_failure": "runtime_smoke",
        "runtime_smoke_details": {
            "classification": classification,
            "log_tail": "generic boot error with no source file reference",
        },
        "failed_subtask_ids": [],
        "subtask_results": {},
        "plan": TaskPlan(subtasks=[]),
        "replan_count": replan_count,
        "runtime_smoke_last_signature": last_signature,
        "project_id": "",
    }


def _attributed_state(*, blame="st-a", last_signature="", replan_count=0):
    """log_tail 引用某写者 scope → 归因到该子任务 → 走定向 retry；签名含 fid。"""
    subtasks = [
        _st("st-a", writable=["src/a.py"]),
        _st("st-b", writable=["src/b.py"]),
    ]
    ref = "src/a.py" if blame == "st-a" else "src/b.py"
    sibling = "st-b" if blame == "st-a" else "st-a"
    return {
        "verification_failure": "runtime_smoke",
        "runtime_smoke_details": {
            "classification": "code_error",
            "log_tail": f"Traceback: File \"{ref}\", line 12, in handler\n    boom",
        },
        "failed_subtask_ids": [blame],
        # 归因要求 failed 是 subtask_results 键的【真子集】（至少留一个成功兄弟），故补 sibling
        "subtask_results": {blame: _wo(blame, l1_passed=False), sibling: _wo(sibling)},
        "plan": TaskPlan(subtasks=subtasks),
        "replan_count": replan_count,
        "runtime_smoke_last_signature": last_signature,
        "project_id": "",
    }


# ─────────────────────────────────────────────────────────────
# 1. 签名采集 — 首轮无 plateau，存签名
# ─────────────────────────────────────────────────────────────

def test_first_runtime_failure_stores_signature_no_plateau():
    """首轮（无上一轮签名）→ 非 plateau，正常 replan，存下本轮签名。"""
    result = _run(_unattributed_state(last_signature=""))
    assert result.get("failure_strategy") == "replan"
    assert result.get("failure_escalated") is False
    assert result.get("runtime_smoke_last_signature") == "code_error|"


def test_attributed_signature_includes_blamed_subtasks():
    """归因到写者 → 定向 retry，签名含 fid（classification|st-a）。"""
    result = _run(_attributed_state(blame="st-a", last_signature=""))
    assert result.get("targeted_recovery") is True, (
        f"应归因到 st-a 走定向 retry；实际 strategy={result.get('failure_strategy')}"
    )
    assert result.get("runtime_smoke_last_signature") == "code_error|st-a"


# ─────────────────────────────────────────────────────────────
# 2. plateau 观测（默认）— 控制流不变
# ─────────────────────────────────────────────────────────────

def test_plateau_observed_default_keeps_normal_flow():
    """连续两轮同签名、非 strict → 仅观测：控制流不变（仍 replan，不提前 escalate）。"""
    result = _run(_unattributed_state(last_signature="code_error|"))
    assert result.get("failure_strategy") == "replan"
    assert result.get("failure_escalated") is False
    assert result.get("runtime_smoke_last_signature") == "code_error|"
    # 观测模式绝不污染 degraded（避免给"隐性收敛后恢复成功"的交付误挂 plateau 痕迹）
    assert "runtime_smoke_plateau:code_error" not in (result.get("degraded_reasons") or [])


def test_plateau_observed_default_when_strict_env_off(monkeypatch):
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PLATEAU_STRICT", "0")
    result = _run(_unattributed_state(last_signature="code_error|"))
    assert result.get("failure_strategy") == "replan"
    assert result.get("failure_escalated") is False


# ─────────────────────────────────────────────────────────────
# 3. plateau strict — 短路提前 escalate
# ─────────────────────────────────────────────────────────────

def test_plateau_strict_short_circuits_to_escalate(monkeypatch):
    """strict 开 + 连续同签名 → 短路提前 escalate（省无谓重试），留 degraded 痕迹。"""
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PLATEAU_STRICT", "1")
    result = _run(_unattributed_state(last_signature="code_error|"))
    assert result.get("failure_strategy") == "escalate"
    assert result.get("failure_escalated") is True
    assert result.get("runtime_smoke_passed") is False
    assert "runtime_smoke_plateau:code_error" in (result.get("degraded_reasons") or [])
    assert result.get("runtime_smoke_last_signature") == "code_error|"


def test_plateau_strict_still_bounded_by_replan_limit_first(monkeypatch):
    """replan 已达上限时，上方 max-iteration 熔断先触发 escalate（plateau 不改变有界性）。"""
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PLATEAU_STRICT", "1")
    # replan_count=2, max_retries 默认 2 → _rt_replan=3>2 → 走上方上限 escalate（在 plateau 之前）
    result = _run(_unattributed_state(last_signature="code_error|", replan_count=2))
    assert result.get("failure_strategy") == "escalate"
    assert result.get("failure_escalated") is True


# ─────────────────────────────────────────────────────────────
# 4. 进展敏感 — 签名不同不算 plateau（绝不误伤真进展）
# ─────────────────────────────────────────────────────────────

def test_no_plateau_when_classification_differs(monkeypatch):
    """本轮 classification 与上轮不同 → 失败形态变了=有进展，非 plateau。"""
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PLATEAU_STRICT", "1")
    result = _run(_unattributed_state(classification="code_error", last_signature="env_missing|"))
    assert result.get("failure_strategy") == "replan"  # 非 escalate
    assert result.get("failure_escalated") is False
    assert result.get("runtime_smoke_last_signature") == "code_error|"


def test_no_plateau_when_blamed_subtasks_differ(monkeypatch):
    """同 classification 但归因子任务集不同（上轮 st-a 修好、本轮换 st-b 失败）=有进展，非 plateau。"""
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PLATEAU_STRICT", "1")
    result = _run(_attributed_state(blame="st-b", last_signature="code_error|st-a"))
    assert result.get("failure_escalated") is False, (
        "归因子任务集变化=真进展，strict 也绝不误判 plateau 提前 escalate"
    )
    assert result.get("runtime_smoke_last_signature") == "code_error|st-b"


# ─────────────────────────────────────────────────────────────
# 5. 复核整改：跨 REVISE 边界重置 + 粒度权衡文档化
# ─────────────────────────────────────────────────────────────

def test_revision_resets_plateau_signature():
    """复核 B：人工 REVISE=全新一轮，revision 节点必须清 plateau 签名，防跨轮 staleness 误判。"""
    from swarm.brain.nodes import revision

    state = {
        "plan": TaskPlan(subtasks=[_st("st-a", writable=["src/a.py"])]),
        "revision_feedback": "改这里",
        "subtask_results": {},
        "runtime_smoke_last_signature": "code_error|st-a",  # 上一轮终态遗留
        "project_id": "",
    }

    async def _fake_invoke(self, msgs):
        class R:
            content = '{"revision_subtasks": [{"id": "rev-1", "description": "d"}]}'

        return R()

    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        inst = mock_llm.return_value
        inst.ainvoke = _fake_invoke.__get__(inst)
        out = asyncio.run(revision(state))
    assert out.get("runtime_smoke_last_signature") == "", (
        "revision 必须把 plateau 签名清空（与 subtask_retry_counts/failure_escalated 同属'新一轮重置'集）"
    )


def test_plateau_strict_coarse_same_subtask_is_intended(monkeypatch):
    """复核 A（文档化权衡·刻意）：同子任务同 classification 但底层 bug 不同 → 签名相同 →
    strict 判 plateau 提前【转人工】。这是已知粒度权衡（不掺易抖动的 log 指纹防检测器变哑），
    最坏=早一轮转人工审核（可恢复，绝不发坏码），非缺陷。本测试钉住此意图防未来误改。"""
    monkeypatch.setenv("SWARM_RUNTIME_SMOKE_PLATEAU_STRICT", "1")
    # 两轮都归因 st-a、都 code_error（底层 bug 可能已不同，但签名粒度看不出）
    result = _run(_attributed_state(blame="st-a", last_signature="code_error|st-a"))
    assert result.get("failure_strategy") == "escalate"
    assert result.get("failure_escalated") is True
    assert "runtime_smoke_plateau:code_error" in (result.get("degraded_reasons") or [])
