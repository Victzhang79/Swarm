"""R65E9-T1（round65e9 FAILED@PLAN 三路定案·下游机制根）：baseline_ineligible 确定性 pin。

死因：证据闸拒掉假 baseline_covered 后【记忆缺失】（baseline_covered=last-write-wins/
feedback=oneshot）→被拒 req 陷 limbo（矩阵不带 vocab→假 baseline 算"已覆盖"→非 uncovered；
但校验层出"缺证据"issue→invalid）→L2 file-replan 只读 uncovered→跳过它→planner 每 retry 重
declare 同一 req（req-feaae262 Redis 诊断，基线真无 Redis）→死钉耗尽 3-retry→FAILED@PLAN。

治：build_coverage_matrix 新增 baseline_ineligible 参数——无条件把 pinned id 踢出合法 baseline→
落 uncovered→逼建 covers 子任务（进 L2 replan）；validate_plan 每轮把新拒的假 baseline 单调累积
进 state.baseline_ineligible_reqs，立即生效打断 limbo。
"""
from __future__ import annotations

import swarm.brain.nodes as nodes
from swarm.brain.nodes import _r65e9_ineligible_feedback, validate_plan
from swarm.brain.plan_validator import (
    build_coverage_matrix,
    validate_requirement_coverage,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content='{"valid": true, "issues": []}'):
        self._content = content
        self.captured = []

    async def ainvoke(self, messages):
        self.captured.append(messages[1]["content"])
        return _Resp(self._content)

REQ_A = "req-aaaa1111"
REQ_B = "req-bbbb2222"


def _items():
    return [
        {"id": REQ_A, "text": "系统提供 Redis 诊断接口用于观测", "kind": "functional"},
        {"id": REQ_B, "text": "系统支持条目二的数据约束", "kind": "data"},
    ]


def _st(sid, covers=None):
    return SubTask(id=sid, description="do", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=["a"], readable=[]), covers=list(covers or []))


def _plan(*sts):
    return TaskPlan(subtasks=list(sts), parallel_groups=[[s.id] for s in sts])


# ── 核心：pinned baseline 被无条件踢出→落 uncovered ──
def test_pinned_baseline_forced_uncovered():
    """★RED 核★ REQ_A 被申报 baseline_covered（有 reason），但已 pin 为不合格 →
    无条件踢出 baseline → 落 uncovered（逼建 covers 子任务）。"""
    p = _plan(_st("st-1", covers=[REQ_B]))  # 只覆盖 B，A 靠 baseline 申报
    bc = [{"id": REQ_A, "reason": "现有代码已提供 Redis 诊断"}]
    # 无 pin：A 走 baseline → 覆盖满足、uncovered 空
    m0 = build_coverage_matrix(p, _items(), baseline_covered=bc)
    assert [u["id"] for u in m0["uncovered"]] == []
    assert any(e["id"] == REQ_A for e in m0["baseline_covered"])
    # 有 pin：A 被无条件踢出 baseline → uncovered
    m1 = build_coverage_matrix(p, _items(), baseline_covered=bc, baseline_ineligible=[REQ_A])
    assert [u["id"] for u in m1["uncovered"]] == [REQ_A], "pinned baseline 必须落 uncovered"
    assert not any(e["id"] == REQ_A for e in m1["baseline_covered"]), "pinned 不得留在合法 baseline"


def test_pinned_but_really_covered_stays_covered():
    """pin 只从 baseline 剔除，绝不 un-cover 真被子任务 covers 的 req（不误伤真覆盖）。"""
    p = _plan(_st("st-1", covers=[REQ_A, REQ_B]))
    bc = [{"id": REQ_A, "reason": "x"}]
    m = build_coverage_matrix(p, _items(), baseline_covered=bc, baseline_ineligible=[REQ_A])
    assert m["uncovered"] == [], "被真 covers 的 req 即便 pinned 也仍算覆盖"
    assert {it["id"]: it["covered_by"] for it in m["items"]}[REQ_A] == ["st-1"]


def test_no_pin_backward_compatible():
    """缺省 baseline_ineligible=None → 逐字节向后兼容（既有调用点行为不变）。"""
    p = _plan(_st("st-1", covers=[REQ_B]))
    bc = [{"id": REQ_A, "reason": "x"}]
    m = build_coverage_matrix(p, _items(), baseline_covered=bc)
    assert any(e["id"] == REQ_A for e in m["baseline_covered"])
    assert [u["id"] for u in m["uncovered"]] == []


def test_pin_unconditional_over_evidence():
    """★关键★ pin 优先于证据：即便 vocab 判其【有证据】，pinned 仍被踢（无条件）。"""
    p = _plan(_st("st-1", covers=[REQ_B]))
    bc = [{"id": REQ_A, "reason": "现有 Redis 诊断代码"}]
    vocab = "redis 诊断 观测 接口"  # 假装基线 vocab 命中 → 证据闸本会放行
    m = build_coverage_matrix(p, _items(), baseline_covered=bc,
                              baseline_vocab=vocab, baseline_ineligible=[REQ_A])
    assert [u["id"] for u in m["uncovered"]] == [REQ_A], "pin 必须无条件覆盖证据判定"


# ── validate_requirement_coverage 出 uncovered issue ──
def test_validate_pinned_yields_uncovered_issue():
    p = _plan(_st("st-1", covers=[REQ_B]))
    bc = [{"id": REQ_A, "reason": "x"}]
    r = validate_requirement_coverage(p, _items(), baseline_covered=bc,
                                      baseline_ineligible=[REQ_A])
    assert not r.valid
    assert any(REQ_A in str(i) and "未被任何子任务覆盖" in str(i) for i in r.issues), \
        f"pinned req 应出'未覆盖·分配子任务'出口: {r.issues}"


# ── _r65e9_ineligible_feedback ──
def test_ineligible_feedback_lists_ids_and_ban():
    fb = _r65e9_ineligible_feedback([REQ_A, REQ_B])
    assert REQ_A in fb and REQ_B in fb
    assert "禁止" in fb and "baseline_covered" in fb and "子任务" in fb


def test_ineligible_feedback_empty():
    assert _r65e9_ineligible_feedback([]) == ""
    assert _r65e9_ineligible_feedback(None) == ""


# ── state key 单调语义 ──
def test_baseline_ineligible_reqs_is_monotonic():
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE
    assert ACCOUNTING_KEY_LIFECYCLE.get("baseline_ineligible_reqs") == "monotonic", \
        "拒绝集必须单调累积（否则跨 retry 丢失=死钉复发）"


# ── ★节点集成·收敛保证★ validate_plan emit + 累积 enforce ──
def _clean_env(monkeypatch):
    monkeypatch.delenv("SWARM_VALIDATE_PLAN_LLM_GATE", raising=False)
    monkeypatch.delenv("SWARM_PLAN_COVERAGE_GATE", raising=False)


async def test_node_emits_ineligible_on_false_baseline(monkeypatch):
    """★核心 RED★ planner 谎称 REQ_A 存量（vocab 无证据）→ 节点当轮拒 + emit
    baseline_ineligible_reqs=[REQ_A] + 立即把 REQ_A 逼成 uncovered（打断 limbo）。"""
    _clean_env(monkeypatch)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    # vocab 非空但不含 Redis 判别术语 → REQ_A 的 baseline 申报被证据闸判假
    async def _fake_vocab(_state):
        return "usercontroller loginservice sysuser mapper"
    monkeypatch.setattr(nodes, "_baseline_vocab_for", _fake_vocab)
    out = await validate_plan({
        "plan": _plan(_st("st-1", covers=[REQ_B])),   # 只覆盖 B；A 靠假 baseline 申报
        "task_description": "t", "complexity": "medium", "plan_retry_count": 0,
        "requirement_items": _items(),
        "baseline_covered": [{"id": REQ_A, "reason": "现有代码已提供 Redis 诊断（谎称）"}],
    })
    assert out["plan_valid"] is False
    assert REQ_A in (out.get("baseline_ineligible_reqs") or []), \
        f"假 baseline 应入不合格集: {out.get('baseline_ineligible_reqs')}"
    # 立即生效：REQ_A 被逼成"未覆盖·分配子任务"，feedback 明确禁止再申报 baseline
    fb = out["plan_validation_feedback"]
    assert REQ_A in fb and ("禁止" in fb or "未被任何子任务覆盖" in fb)


async def test_node_accumulated_pin_forces_uncovered_no_a6(monkeypatch):
    """★收敛核★ 累积 pin（state 带 REQ_A）+ 本轮 planner 再谎称存量（vocab 恰命中）→ 无条件踢→
    uncovered。retry_count=0 关掉 A6→plan_valid=False + REQ_A 入 feedback（不再陷 baseline limbo）。"""
    _clean_env(monkeypatch)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    async def _fake_vocab(_state):
        return "redis 诊断 观测"   # 本轮 vocab 恰含术语（证据闸本会放行）——但 pin 无条件踢
    monkeypatch.setattr(nodes, "_baseline_vocab_for", _fake_vocab)
    out = await validate_plan({
        "plan": _plan(_st("st-1", covers=[REQ_B])),
        "task_description": "t", "complexity": "medium", "plan_retry_count": 0,
        "requirement_items": _items(),
        "baseline_covered": [{"id": REQ_A, "reason": "redis 诊断观测"}],
        "baseline_ineligible_reqs": [REQ_A],   # 往轮已钉
    })
    assert out["plan_valid"] is False, "pinned req 即便本轮有证据也必须被踢→uncovered→invalid"
    assert REQ_A in out["plan_validation_feedback"]


async def test_node_pin_converts_death_to_documented_gap(monkeypatch):
    """★round65e9 死因修复实证★ 假 baseline（本会 FAILED@PLAN 死钉）→ 本 fix 令其落 uncovered→
    retry≥1 小缺口下 A6 优雅放行：plan 进执行期 + REQ_A 进 coverage_gap_residual（可见记录，
    非静默谎称存量丢交付）。这正是 round65e9 从 FAILED→graceful 的转变。"""
    _clean_env(monkeypatch)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    async def _fake_vocab(_state):
        return "usercontroller loginservice"   # 无 redis 证据 → 证据闸判假
    monkeypatch.setattr(nodes, "_baseline_vocab_for", _fake_vocab)
    out = await validate_plan({
        "plan": _plan(_st("st-1", covers=[REQ_B])),
        "task_description": "t", "complexity": "medium", "plan_retry_count": 1,
        "requirement_items": _items(),
        "baseline_covered": [{"id": REQ_A, "reason": "谎称 Redis 诊断存量"}],
    })
    assert out["plan_valid"] is True, "★死因修复★ 小缺口 A6 优雅放行进执行，不再 FAILED@PLAN"
    assert REQ_A in (out.get("coverage_gap_residual") or []), \
        f"假 baseline 应变【可见缺口】非静默丢: {out.get('coverage_gap_residual')}"
    # ★复核 F1（HIGH）回归锁★ A6 放行路径必须【也】持久化 pin，否则后续 replan 从 A6 门复发打地鼠
    assert REQ_A in (out.get("baseline_ineligible_reqs") or []), \
        f"A6 放行路径必须持久化不合格集（防 replan 复发）: {out.get('baseline_ineligible_reqs')}"
