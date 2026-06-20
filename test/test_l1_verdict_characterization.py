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


def test_char_phase3_refusal_overrides_det_true():
    # refusal 覆盖 det True → fail
    assert _phase3_current(det_ok=True, llm_passed=True, verify_text="Sorry, need more steps to process") == (False, "refusal_hard_fail")


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
def _phase4_current(*, det_ok, llm_ok, prior_passed) -> bool:
    if det_ok is False:
        return False
    if det_ok is True:
        if not llm_ok:
            return False
        # llm_ok True：循环内 fail → 翻盘；否则维持
        return True
    # det_ok is None：维持 prior，不主动翻盘
    return prior_passed


def test_char_phase4_det_false_no_flip():
    # 即便循环内 pass，det False 也压回 False
    assert _phase4_current(det_ok=False, llm_ok=True, prior_passed=True) is False


def test_char_phase4_det_true_llm_false_fails():
    assert _phase4_current(det_ok=True, llm_ok=False, prior_passed=True) is False


def test_char_phase4_det_true_llm_true_flips_prior_fail():
    # 关键：当前实现【无条件】翻盘——prior fail + det True + llm True → True
    assert _phase4_current(det_ok=True, llm_ok=True, prior_passed=False) is True


def test_char_phase4_det_true_llm_true_maintains_prior_pass():
    assert _phase4_current(det_ok=True, llm_ok=True, prior_passed=True) is True


def test_char_phase4_det_none_maintains_prior():
    assert _phase4_current(det_ok=None, llm_ok=True, prior_passed=False) is False
    assert _phase4_current(det_ok=None, llm_ok=True, prior_passed=True) is True


# ────────────────────────────────────────────────────────────────────
# refusal 检测当前行为（钉住 Commit ① 前的 _is_refusal_or_truncated）
# ────────────────────────────────────────────────────────────────────
def test_char_refusal_markers_current():
    assert _is_refusal_or_truncated("Sorry, need more steps to process this") is True
    assert _is_refusal_or_truncated("I'm unable to do that") is True
    assert _is_refusal_or_truncated("cannot complete this request") is True


def test_char_refusal_empty_current():
    # Commit ① 前：空/纯空格 → False（非拒答）
    assert _is_refusal_or_truncated("") is False
    assert _is_refusal_or_truncated("   ") is False


def test_char_refusal_normal_text_current():
    assert _is_refusal_or_truncated("L1_RESULT: PASS, compiled fine") is False


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
