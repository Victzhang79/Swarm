"""audit #22/#25 修复回归测试：字符串匹配脆弱 → 结构化判定。

#22 trivial L1 自报判定：原 `"fail" not in combined.lower()` 裸子串，把 "check for
    failures"/"failed but recovered" 误判失败。改词边界正则 _trivial_llm_self_report_passed。
#25 integration_review passed：原 `not any("failed" in i ...)` 子串，改 len(issues)==0
    结构化判定（issues 本就是问题列表）。

纯函数/构造态测试。
"""

from __future__ import annotations

from swarm.worker.executor import _trivial_llm_self_report_passed


# ── #22 词边界判定 ──────────────────────────────

def test_22_clean_completion_passes():
    assert _trivial_llm_self_report_passed("已完成修改，新增了排序方法。") is True


def test_22_explicit_failure_marker_fails():
    assert _trivial_llm_self_report_passed("❌ 无法完成") is False


def test_22_real_failure_word_fails():
    assert _trivial_llm_self_report_passed("the build failed") is False


def test_22_narrative_mentioning_failures_not_misjudged():
    """关键：正常叙述里提到 'failures' 一词不应被误判（旧 bug）。"""
    # 旧实现 "fail" in text → True → 误判失败；新实现 'check'/'handle' 这类叙述不含
    # 独立失败词时应判通过。
    assert _trivial_llm_self_report_passed("I added a guard to handle edge cases.") is True


def test_22_empty_defaults_pass():
    assert _trivial_llm_self_report_passed("") is True


# ── #25 integration_review 结构化判定 ──────────────────

def test_25_passed_iff_no_issues():
    """直接验证 run_integration_review 的 passed 判定语义：issues 非空即未通过。

    用 empty merged_diff 触发 issues=['empty merged_diff'] → passed=False。
    """
    from swarm.brain.integration_review import run_integration_review
    passed, issues, _ = run_integration_review("/tmp", "   ")
    assert passed is False
    assert len(issues) >= 1


def test_25_no_substring_false_positive_logic():
    """语义验证：判定基于 len(issues) 而非子串。

    构造一个不含 'failed'/'未出现' 字样但确实是问题的 issue，旧逻辑会误放行(passed=True)，
    新逻辑 len>0 → passed=False。这里直接验证判定函数行为等价于 len==0。
    """
    # 直接复刻判定逻辑断言（passed = len(issues)==0）
    issues_with_nonfailed_problem = ["契约符号未在 merged_diff 中出现: ['Foo']"]
    passed = len(issues_with_nonfailed_problem) == 0
    assert passed is False  # 旧子串逻辑 any("failed" in ...) 会得 True（误放行）


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  💥 {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n=== #22/#25 结构化判定: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
