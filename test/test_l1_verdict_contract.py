#!/usr/bin/env python3
"""W1.2 commit② 契约测试 — 单一 L1 仲裁器 evaluate_l1 的真值表 + 翻盘门槛。

钉死 LOCKED CONTRACT：决策顺序（首个命中即返回）：
  1. refusal/截断 → False, source=refusal_hard_fail, sticky=True（覆盖一切，含 det True）。永不翻盘。
  2. det_ok False → False, sticky=True, source 携带确定性失败原因。永不翻盘
     （例外：empty_diff_transient sticky=False 可翻盘）。
  3. det_ok None → passed=llm_self_report, source=llm_self_report, sticky=False；不主动翻盘 prior fail。
  4. det_ok True → 看 llm_ok：
       llm_ok False → False（证据冲突）。
       llm_ok True → prior None/True→True；prior False 仅当 sticky False 且
                     source∈{empty_diff_transient, llm_self_report} 才翻盘 True，否则维持 False。

净收益（关闭幻觉 PASS）的核心断言：refusal / 编译失败 / scope 违规 / 测试失败的 prior
在 Phase-4 det True + llm True 下【不再翻盘】（旧实现无条件翻盘）。
"""
from __future__ import annotations

from swarm.worker.executor import L1Verdict, evaluate_l1


# ── 规则 1：refusal 最高优先，覆盖 det True ──
def test_refusal_overrides_det_true():
    v = evaluate_l1(det_ok=True, det_details={}, verify_result="Sorry, need more steps to process",
                    llm_ok=True, prior=None, phase="x")
    assert v.passed is False
    assert v.source == "refusal_hard_fail"
    assert v.sticky is True


def test_chinese_refusal_overrides_det_true():
    v = evaluate_l1(det_ok=True, det_details={}, verify_result="抱歉，我无法完成这个任务",
                    llm_ok=True, prior=None, phase="x")
    assert v.passed is False and v.source == "refusal_hard_fail" and v.sticky is True


# ── 规则 2：det False 各原因映射 + sticky ──
def test_det_false_compile_sticky():
    v = evaluate_l1(det_ok=False, det_details={"l1_2_compile_ok": False, "compile_message": "SyntaxError"},
                    verify_result="L1_RESULT: PASS", llm_ok=True, prior=None, phase="x")
    assert v.passed is False and v.source == "compile" and v.sticky is True


def test_det_false_scope_sticky():
    v = evaluate_l1(det_ok=False, det_details={"scope_violations": ["a.py"]},
                    verify_result="ok done here", llm_ok=True, prior=None, phase="x")
    assert v.passed is False and v.source == "scope" and v.sticky is True


def test_det_false_test_sticky():
    v = evaluate_l1(det_ok=False, det_details={"l1_3_test_ok": False},
                    verify_result="ran tests ok", llm_ok=True, prior=None, phase="x")
    assert v.passed is False and v.source == "test" and v.sticky is True


def test_det_false_lint_sticky():
    v = evaluate_l1(det_ok=False, det_details={"lint": {"has_error": True, "gated": True}},
                    verify_result="lint clean ok", llm_ok=True, prior=None, phase="x")
    assert v.passed is False and v.source == "lint" and v.sticky is True


def test_det_false_empty_diff_transient_flippable():
    v = evaluate_l1(det_ok=False, det_details={"reason": "empty_diff_but_changes_expected"},
                    verify_result="working on it now", llm_ok=True, prior=None, phase="x")
    assert v.passed is False
    assert v.source == "empty_diff_transient"
    assert v.sticky is False  # 唯一可翻盘的 det fail


# ── 规则 3：det None → LLM 自报，不翻盘 prior fail ──
def test_det_none_uses_llm_self_report():
    assert evaluate_l1(det_ok=None, det_details={}, verify_result="L1_RESULT: PASS",
                       llm_ok=True, prior=None, phase="x").passed is True
    assert evaluate_l1(det_ok=None, det_details={}, verify_result="L1_RESULT: FAIL",
                       llm_ok=False, prior=None, phase="x").passed is False


def test_det_none_does_not_flip_prior_fail():
    prior = L1Verdict(passed=False, source="llm_self_report", sticky=False)
    v = evaluate_l1(det_ok=None, det_details={}, verify_result="seems fine now",
                    llm_ok=True, prior=prior, phase="x")
    assert v.passed is False  # 缺确定性证据，不翻盘


# ── 规则 4：det True + llm 冲突 / 翻盘门槛 ──
def test_det_true_llm_false_conflict_fail():
    v = evaluate_l1(det_ok=True, det_details={}, verify_result="L1_RESULT: PASS",
                    llm_ok=False, prior=None, phase="x")
    assert v.passed is False and v.source == "deterministic_llm_conflict"


def test_det_true_llm_true_no_prior_pass():
    v = evaluate_l1(det_ok=True, det_details={}, verify_result=None, llm_ok=True, prior=None, phase="x")
    assert v.passed is True and v.source == "deterministic"


def test_det_true_llm_true_maintains_prior_pass():
    prior = L1Verdict(passed=True, source="deterministic", sticky=False)
    assert evaluate_l1(det_ok=True, det_details={}, verify_result=None, llm_ok=True,
                       prior=prior, phase="x").passed is True


# ── 翻盘门槛：可翻盘来源 vs 不可翻盘（净收益核心）──
def test_flip_empty_diff_transient():
    prior = L1Verdict(passed=False, source="empty_diff_transient", sticky=False)
    v = evaluate_l1(det_ok=True, det_details={}, verify_result=None, llm_ok=True, prior=prior, phase="x")
    assert v.passed is True  # 设计上唯一应翻盘的情形


def test_flip_llm_self_report():
    prior = L1Verdict(passed=False, source="llm_self_report", sticky=False)
    v = evaluate_l1(det_ok=True, det_details={}, verify_result=None, llm_ok=True, prior=prior, phase="x")
    assert v.passed is True


def test_no_flip_refusal_prior():
    """净收益①：refusal prior 在 det True + llm True 下【不再翻盘】（旧实现会翻成 PASS）。"""
    prior = L1Verdict(passed=False, source="refusal_hard_fail", sticky=True)
    v = evaluate_l1(det_ok=True, det_details={}, verify_result=None, llm_ok=True, prior=prior, phase="x")
    assert v.passed is False


def test_no_flip_compile_prior():
    """净收益②：编译失败 prior 不再翻盘。"""
    prior = L1Verdict(passed=False, source="compile", sticky=True)
    v = evaluate_l1(det_ok=True, det_details={}, verify_result=None, llm_ok=True, prior=prior, phase="x")
    assert v.passed is False


def test_no_flip_scope_prior():
    """净收益③：scope 违规 prior 不再翻盘。"""
    prior = L1Verdict(passed=False, source="scope", sticky=True)
    v = evaluate_l1(det_ok=True, det_details={}, verify_result=None, llm_ok=True, prior=prior, phase="x")
    assert v.passed is False


def test_no_flip_test_prior():
    """净收益④：测试失败 prior 不再翻盘。"""
    prior = L1Verdict(passed=False, source="test", sticky=True)
    v = evaluate_l1(det_ok=True, det_details={}, verify_result=None, llm_ok=True, prior=prior, phase="x")
    assert v.passed is False


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {fn.__name__}: {e}")
            fails += 1
    sys.exit(1 if fails else 0)
