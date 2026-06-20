#!/usr/bin/env python3
"""WorkerExecutor L1 断言强化单元测试（借鉴 ECC 确定性闸门思路）。

聚焦验证 _parse_l1_result 的鲁棒性 —— 不再被脆弱的字符串/中文子串匹配误导。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _executor_stub():
    """用 __new__ 绕过 __init__（避免加载配置/沙箱），仅测纯解析方法。"""
    from swarm.worker.executor import WorkerExecutor

    return WorkerExecutor.__new__(WorkerExecutor)


def test_parse_explicit_pass():
    """显式 L1_RESULT: PASS 标记 → 通过。"""
    ex = _executor_stub()
    passed, details = ex._parse_l1_result("验证完成。L1_RESULT: PASS 编译通过测试通过")
    assert passed is True
    assert details["llm_self_report"] == "pass"
    print("  ✅ 显式 PASS 标记识别")


def test_parse_explicit_fail():
    """显式 L1_RESULT: FAIL 标记 → 未通过。"""
    ex = _executor_stub()
    passed, details = ex._parse_l1_result("L1_RESULT: FAIL 编译报错")
    assert passed is False
    assert details["llm_self_report"] == "fail"
    print("  ✅ 显式 FAIL 标记识别")


def test_parse_case_insensitive():
    """大小写/空格容忍：l1_result:pass。"""
    ex = _executor_stub()
    passed, _ = ex._parse_l1_result("结果 l1_result : pass")
    assert passed is True
    print("  ✅ 大小写空格容忍")


def test_parse_no_marker_conservative_fail():
    """无显式标记 + 出现失败信号 → 保守判定未通过（旧实现会误判）。"""
    ex = _executor_stub()
    # 旧实现：'L1_RESULT: PASS' not in text → False，但 '编译'+'通过' 子串会误报 compile_passed
    passed, details = ex._parse_l1_result("编译时通过了语法检查，但测试出现 error 失败")
    assert passed is False, "出现 error/失败信号应保守判定未通过"
    print("  ✅ 无标记+失败信号 → 保守未通过")


def test_parse_no_marker_clean_pass():
    """无显式标记 + 纯成功描述 → 通过。"""
    ex = _executor_stub()
    passed, _ = ex._parse_l1_result("一切正常，编译通过，测试全部通过 ✅")
    assert passed is True
    print("  ✅ 无标记+纯成功 → 通过")


def test_parse_mixed_signal_prefers_fail():
    """混合信号（既有通过又有失败）→ 保守判定未通过。"""
    ex = _executor_stub()
    passed, _ = ex._parse_l1_result("编译通过 ✅ 但单元测试失败 ❌")
    assert passed is False, "混合信号应保守判失败（避免幻觉 PASS）"
    print("  ✅ 混合信号 → 保守未通过（拦截幻觉 PASS）")


def test_parse_empty_input():
    """空输入安全处理。

    W1.2 commit②：refusal 硬化后，空回复被判为【不可用】(截断/空转)，
    走 _parse_l1_result 的 llm_unavailable 分支：passed=False、
    llm_self_report='unavailable'。这是契约刻意收紧——空 verify 回复
    不再被当作"有内容的常规保守判定"，而是明确标记不可用。
    """
    ex = _executor_stub()
    passed, details = ex._parse_l1_result("")
    assert passed is False
    assert details["llm_self_report"] == "unavailable"
    print("  ✅ 空输入安全处理（W1.2：标记 unavailable）")


def main() -> int:
    print("\n🧪 WorkerExecutor L1 断言强化 单元测试\n")
    tests = [
        test_parse_explicit_pass,
        test_parse_explicit_fail,
        test_parse_case_insensitive,
        test_parse_no_marker_conservative_fail,
        test_parse_no_marker_clean_pass,
        test_parse_mixed_signal_prefers_fail,
        test_parse_empty_input,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
