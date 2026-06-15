"""P1-2 回归测试：L1 自报解析过滤模型拒答串（消除 raw_result 误导）。

背景（task 0f93f1fc）：MiniMax 模型多轮返回 "Sorry, need more steps to process
this request."，被直接塞进 L1 详情 raw_result，与 deterministic_gate 结果混在一起，
让人误读为"验证已执行但失败"。修复：识别拒答/截断，标记 llm_self_report=unavailable。
"""
from __future__ import annotations

from swarm.worker.executor import WorkerExecutor


def _parse(text):
    # _parse_l1_result 不依赖实例状态，用 __new__ 绕过 __init__ 直接调。
    inst = WorkerExecutor.__new__(WorkerExecutor)
    return WorkerExecutor._parse_l1_result(inst, text)


def test_refusal_marked_unavailable():
    passed, details = _parse("Sorry, need more steps to process this request.")
    assert passed is False
    assert details["llm_self_report"] == "unavailable", details
    assert "拒答" in details["raw_result"] or "截断" in details["raw_result"]
    # 原始拒答内容保留在 raw_refusal 供调试，但不污染 raw_result
    assert "need more steps" in details["raw_refusal"].lower()
    print("  ✅ L1 解析: 模型拒答串标记 unavailable，不污染 raw_result")


def test_explicit_pass_still_works():
    passed, details = _parse("## L1_RESULT: PASS\n编译通过，测试通过")
    assert passed is True
    assert details["llm_self_report"] == "pass"
    print("  ✅ L1 解析: 显式 PASS 正常识别")


def test_explicit_fail_still_works():
    passed, details = _parse("## L1_RESULT: FAIL\n编译失败")
    assert passed is False
    assert details["llm_self_report"] == "fail"
    assert details["raw_result"].startswith("## L1_RESULT")  # 正常内容保留
    print("  ✅ L1 解析: 显式 FAIL 正常识别，raw_result 保留真实内容")


def test_empty_not_treated_as_refusal():
    passed, details = _parse("")
    # 空串不是拒答，走常规保守判定
    assert details["llm_self_report"] in ("fail", "pass")
    assert details.get("raw_refusal") is None
    print("  ✅ L1 解析: 空串不误判为拒答")


if __name__ == "__main__":
    tests = [
        test_refusal_marked_unavailable,
        test_explicit_pass_still_works,
        test_explicit_fail_still_works,
        test_empty_not_treated_as_refusal,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n=== P1-2 L1 拒答过滤: {passed}/{passed+failed} passed ===")
    import sys
    sys.exit(1 if failed else 0)
