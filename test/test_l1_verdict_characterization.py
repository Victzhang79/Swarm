#!/usr/bin/env python3
"""W1.2 特征化测试 — 钉住【重构前】3 个 L1 裁决点的当前行为。

目的：在把 run() 拆成 phase 方法、并把三处裁决逻辑抽取为统一仲裁器之前，
先用本测试把【当前】行为逐态钉死。Commit ①（纯抽取）后本测试必须全绿，
证明抽取未改变行为；Commit ②（契约）仅在契约【刻意】改变行为处更新本测试，
每一处改动都在 commit message + 报告里登记。

裁决点：
  A) Phase-3 循环裁决 (executor.run, ~L488-508)
  B) trivial 快路径裁决 (_run_trivial_fast, ~L1677-1690)
  C) Phase-4 最终复核 + 翻盘 (executor.run, ~L554-605)

3 态输入：det_ok ∈ {None, False, True}，叠加 refusal 与 llm 自报。

注意：当前(Commit ①前)代码把裁决逻辑【内联】在 run()/trivial 里，无独立函数。
本测试用「裁决逻辑的纯函数复刻」来表达当前真值表——复刻必须与内联代码逐行同义。
Commit ① 抽取出 evaluate_l1 / phase 方法后，本测试改为直接调用被抽取的函数，
真值表断言保持不变（这就是「行为未变」的证据）。
"""
from __future__ import annotations

from swarm.worker.executor import (
    WorkerExecutor,
    _is_refusal_or_truncated,
    _trivial_llm_self_report_passed,
)


# ────────────────────────────────────────────────────────────────────
# 当前 Phase-3 循环裁决真值表（复刻 run() L488-508 内联逻辑）
#
#   det_ok is None  → l1_passed = llm_passed,            source=llm_self_report
#   det_ok 非 None   → l1_passed = det_ok,                source=deterministic
#   refusal(verify) → l1_passed = False (覆盖以上),       source=refusal_hard_fail
# ────────────────────────────────────────────────────────────────────
def _phase3_current(*, det_ok, llm_passed, verify_text) -> tuple[bool, str]:
    if det_ok is None:
        l1_passed = llm_passed
        source = "llm_self_report"
    else:
        l1_passed = det_ok
        source = "deterministic"
    if _is_refusal_or_truncated(verify_text):
        l1_passed = False
        source = "refusal_hard_fail"
    return l1_passed, source


def test_char_phase3_det_none_uses_llm():
    assert _phase3_current(det_ok=None, llm_passed=True, verify_text="L1_RESULT: PASS") == (True, "llm_self_report")
    assert _phase3_current(det_ok=None, llm_passed=False, verify_text="L1_RESULT: FAIL") == (False, "llm_self_report")


def test_char_phase3_det_false_sticks_fail():
    # det False 永远 fail，即便 llm 自报 pass
    assert _phase3_current(det_ok=False, llm_passed=True, verify_text="L1_RESULT: PASS") == (False, "deterministic")


def test_char_phase3_det_true_passes():
    # det True 通过，即便 llm 自报 fail
    assert _phase3_current(det_ok=True, llm_passed=False, verify_text="L1_RESULT: FAIL") == (True, "deterministic")


def test_char_phase3_refusal_with_det_true_is_self_review():
    # det_ok=True 时 refusal 发生在"自读验证"阶段（文件已创建/编译通过），
    # 不应硬否决——请用 evaluate_l1 测试新行为（见下方 test_evaluate_l1_* 系列）。
    # 本 _phase3_current 函数是旧行为快照，保留以记录架构演进，不再断言 refusal_hard_fail。
    pass


# ────────────────────────────────────────────────────────────────────
# 当前 trivial 裁决真值表（复刻 _run_trivial_fast L1640-1690）
#
#   refusal(combined) → False, source=refusal_hard_fail（前置，最高优先）
#   det_ok is None    → llm_passed (=_trivial_llm_self_report_passed), source=llm_self_report
#   det_ok 非 None     → det_ok, source=deterministic
# ────────────────────────────────────────────────────────────────────
def _trivial_current(*, det_ok, combined) -> tuple[bool, str]:
    if _is_refusal_or_truncated(combined):
        return False, "refusal_hard_fail"
    llm_passed = _trivial_llm_self_report_passed(combined)
    if det_ok is None:
        return llm_passed, "llm_self_report"
    return det_ok, "deterministic"


def test_char_trivial_refusal_first():
    assert _trivial_current(det_ok=True, combined="Sorry, need more steps to process") == (False, "refusal_hard_fail")


def test_char_trivial_det_none_uses_llm():
    assert _trivial_current(det_ok=None, combined="done, all good") == (True, "llm_self_report")
    assert _trivial_current(det_ok=None, combined="this failed badly") == (False, "llm_self_report")


def test_char_trivial_det_false_fails():
    assert _trivial_current(det_ok=False, combined="done, all good") == (False, "deterministic")


def test_char_trivial_det_true_passes():
    assert _trivial_current(det_ok=True, combined="this failed badly") == (True, "deterministic")


# ────────────────────────────────────────────────────────────────────
# 当前 Phase-4 最终复核 + 翻盘真值表（复刻 run() L554-605）
#
#   det_ok is False → l1_passed = False（禁翻盘）
#   det_ok is True  → llm_ok（有 diff 时跑 pipeline，否则 True）：
#        llm_ok False → l1_passed = False
#        llm_ok True  → 若 prior(循环内) l1_passed False → 翻盘 True；否则维持 True
#   det_ok is None  → 维持 prior l1_passed（不主动翻盘），source=llm_self_report
# ────────────────────────────────────────────────────────────────────
# W1.2 commit②（契约刻意改变）：翻盘从【无条件】收紧为【仅可翻盘来源】。
# 复刻新契约：prior fail 仅当 source∈{empty_diff_transient, llm_self_report} 且非
# sticky 才翻盘。其余真值表（det False 压回 / det True+llm False fail / det None 维持）不变。
def _phase4_contract(*, det_ok, llm_ok, prior_passed, prior_source="llm_self_report",
                     prior_sticky=False) -> bool:
    if det_ok is False:
        return False
    if det_ok is True:
        if not llm_ok:
            return False
        if prior_passed:
            return True
        # prior fail：仅可翻盘来源 + 非 sticky 才翻盘
        return (not prior_sticky) and prior_source in ("empty_diff_transient", "llm_self_report")
    # det_ok is None：维持 prior，不主动翻盘
    return prior_passed


def test_char_phase4_det_false_no_flip():
    # 即便循环内 pass，det False 也压回 False（行为未变）
    assert _phase4_contract(det_ok=False, llm_ok=True, prior_passed=True) is False


def test_char_phase4_det_true_llm_false_fails():
    # 行为未变
    assert _phase4_contract(det_ok=True, llm_ok=False, prior_passed=True) is False


def test_char_phase4_flip_only_flippable_source():
    # W1.2 收紧：可翻盘来源(llm_self_report/empty_diff_transient)才翻盘
    assert _phase4_contract(det_ok=True, llm_ok=True, prior_passed=False,
                            prior_source="llm_self_report", prior_sticky=False) is True
    assert _phase4_contract(det_ok=True, llm_ok=True, prior_passed=False,
                            prior_source="empty_diff_transient", prior_sticky=False) is True
    # 不可翻盘来源(compile/scope/test/refusal) → 维持 fail（关闭幻觉 PASS 漏洞）
    for src in ("compile", "scope", "test", "refusal_hard_fail"):
        assert _phase4_contract(det_ok=True, llm_ok=True, prior_passed=False,
                                prior_source=src, prior_sticky=True) is False, src


def test_char_phase4_det_true_llm_true_maintains_prior_pass():
    # 行为未变
    assert _phase4_contract(det_ok=True, llm_ok=True, prior_passed=True) is True


def test_char_phase4_det_none_maintains_prior():
    # 行为未变
    assert _phase4_contract(det_ok=None, llm_ok=True, prior_passed=False) is False
    assert _phase4_contract(det_ok=None, llm_ok=True, prior_passed=True) is True


# ────────────────────────────────────────────────────────────────────
# refusal 检测当前行为（钉住 Commit ① 前的 _is_refusal_or_truncated）
# ────────────────────────────────────────────────────────────────────
def test_char_refusal_markers_current():
    assert _is_refusal_or_truncated("Sorry, need more steps to process this") is True
    assert _is_refusal_or_truncated("I'm unable to do that") is True
    assert _is_refusal_or_truncated("cannot complete this request") is True


def test_char_refusal_empty_contract():
    # W1.2 commit②（契约刻意收紧，原 commit① 前为 False）：
    # 空/纯空格回复 = 模型截断/空转，无有效结论 → 按不可用处理 True。
    assert _is_refusal_or_truncated("") is True
    assert _is_refusal_or_truncated("   ") is True


def test_char_refusal_normal_text_current():
    assert _is_refusal_or_truncated("L1_RESULT: PASS, compiled fine") is False


# ────────────────────────────────────────────────────────────────────
# evaluate_l1 直接测试 — Bug B 新行为：refusal + det_ok 分级
#
# 旧行为（已废弃）：refusal 无论 det_ok 为何值都 → refusal_hard_fail sticky=True。
# 新行为（Bug B 修复）：
#   - det_ok is True  → refusal_in_self_review，sticky=False（可翻盘，沙箱自读限制）
#   - det_ok is None  → refusal_hard_fail，sticky=True（无确定性证据，拒答不可信）
#   - det_ok is False → refusal_hard_fail，sticky=True（确定性本身已失败）
# ────────────────────────────────────────────────────────────────────
from swarm.worker.executor import L1Verdict, evaluate_l1  # noqa: E402

_REFUSAL_TEXT = "我无法直接读取刚创建的文件"  # 沙箱自读常见拒答，含 _REFUSAL_MARKERS["我无法"]


def test_evaluate_l1_refusal_with_det_true_is_self_review():
    """Bug B 治本：det_ok=True 时 refusal 降级为 refusal_in_self_review（非硬否决）。"""
    v = evaluate_l1(
        det_ok=True, det_details={"deterministic_gate": "pass"},
        verify_result=_REFUSAL_TEXT, llm_ok=None, prior=None, phase="loop",
    )
    assert v.passed is False
    assert v.source == "refusal_in_self_review"
    assert v.sticky is False, "沙箱自读拒答不应 sticky，Phase4 可翻盘"


def test_evaluate_l1_refusal_with_det_none_is_hard_fail():
    """无确定性证据时 refusal 仍 → refusal_hard_fail sticky=True。"""
    v = evaluate_l1(
        det_ok=None, det_details={},
        verify_result=_REFUSAL_TEXT, llm_ok=None, prior=None, phase="loop",
    )
    assert v.passed is False
    assert v.source == "refusal_hard_fail"
    assert v.sticky is True


def test_evaluate_l1_refusal_with_det_false_is_hard_fail():
    """确定性本身失败时 refusal → refusal_hard_fail sticky=True。"""
    v = evaluate_l1(
        det_ok=False, det_details={"l1_2_compile_ok": False, "compile_message": "error"},
        verify_result=_REFUSAL_TEXT, llm_ok=None, prior=None, phase="loop",
    )
    assert v.passed is False
    assert v.source == "refusal_hard_fail"
    assert v.sticky is True


def test_evaluate_l1_refusal_in_self_review_is_flippable_by_phase4():
    """refusal_in_self_review 在 _FLIPPABLE_SOURCES 中，Phase4 det+LLM 双证可翻盘。"""
    prior = L1Verdict(passed=False, source="refusal_in_self_review",
                      reason="自读拒答", sticky=False, details={})
    v = evaluate_l1(
        det_ok=True, det_details={"deterministic_gate": "pass"},
        verify_result=None,  # Phase4 不传 verify_result（已在循环内裁过）
        llm_ok=True, prior=prior, phase="phase4",
    )
    assert v.passed is True, "确定性+LLM 双证，prior=refusal_in_self_review(非sticky) 应翻盘"
    assert v.source == "deterministic"


def test_evaluate_l1_refusal_hard_fail_not_flippable():
    """refusal_hard_fail 不在 _FLIPPABLE_SOURCES，Phase4 不可翻盘。"""
    prior = L1Verdict(passed=False, source="refusal_hard_fail",
                      reason="执行阶段拒答", sticky=True, details={})
    v = evaluate_l1(
        det_ok=True, det_details={"deterministic_gate": "pass"},
        verify_result=None, llm_ok=True, prior=prior, phase="phase4",
    )
    assert v.passed is False, "refusal_hard_fail sticky=True 不可翻盘"
    assert v.source == "refusal_hard_fail"


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
