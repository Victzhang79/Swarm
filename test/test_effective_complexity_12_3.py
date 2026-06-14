"""12.3 修复回归测试：复杂度真值入口统一（effective_complexity）。

背景：`complexity` 是 analyze 初评，`assessed_complexity` 是澄清后(assess)重评。
历史 bug：after_validate / validate_plan / verify_l2 / handle_failure 读初评 complexity，
导致澄清后升级到 ultra 的任务漏掉 CONFIRM 人工确认闸门、或仍走 SIMPLE 快速路径。

本测试为纯函数/纯路由测试，不触任何存储，无需 DB。
"""

from swarm.brain.graph import after_validate
from swarm.brain.state import effective_complexity
from swarm.types import Complexity


# ── effective_complexity 真值优先级 ──────────────────────────

def test_effective_complexity_prefers_assessed():
    """澄清后定级优先于初评。"""
    state = {"assessed_complexity": Complexity.ULTRA, "complexity": Complexity.MEDIUM}
    assert effective_complexity(state) == Complexity.ULTRA


def test_effective_complexity_falls_back_to_initial():
    """无 assessed 时回退初评。"""
    state = {"complexity": Complexity.COMPLEX}
    assert effective_complexity(state) == Complexity.COMPLEX


def test_effective_complexity_defaults_medium():
    """两者皆无时兜底 MEDIUM。"""
    assert effective_complexity({}) == Complexity.MEDIUM


def test_effective_complexity_assessed_can_downgrade():
    """澄清后降级也应生效（ultra 初评 → simple 重评）。"""
    state = {"assessed_complexity": Complexity.SIMPLE, "complexity": Complexity.ULTRA}
    assert effective_complexity(state) == Complexity.SIMPLE


# ── after_validate 路由：12.3 核心 bug 场景 ──────────────────

def test_after_validate_upgraded_to_ultra_goes_confirm():
    """核心修复点：澄清后升到 ultra（初评仅 medium）的有效计划必须走 CONFIRM 人工确认。

    修复前 after_validate 读初评 complexity=medium → 错误返回 dispatch，漏确认闸门。
    """
    state = {
        "assessed_complexity": Complexity.ULTRA,
        "complexity": Complexity.MEDIUM,
        "plan_valid": True,
        "plan_retry_count": 0,
    }
    assert after_validate(state) == "confirm"


def test_after_validate_downgraded_from_ultra_goes_dispatch():
    """反例：澄清后从 ultra 降到 simple 的有效计划应直接 dispatch，不再强制确认。"""
    state = {
        "assessed_complexity": Complexity.SIMPLE,
        "complexity": Complexity.ULTRA,
        "plan_valid": True,
        "plan_retry_count": 0,
    }
    assert after_validate(state) == "dispatch"


def test_after_validate_non_ultra_dispatches():
    """普通 medium 有效计划直接 dispatch。"""
    state = {"complexity": Complexity.MEDIUM, "plan_valid": True, "plan_retry_count": 0}
    assert after_validate(state) == "dispatch"


def test_after_validate_invalid_retries():
    """计划无效且未达上限 → 重新 plan。"""
    state = {"complexity": Complexity.MEDIUM, "plan_valid": False, "plan_retry_count": 0}
    assert after_validate(state) == "plan"


def test_after_validate_invalid_exhausted_goes_confirm():
    """计划无效且达重试上限 → 升级人工确认。"""
    state = {"complexity": Complexity.MEDIUM, "plan_valid": False, "plan_retry_count": 3}
    assert after_validate(state) == "confirm"


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
    print(f"\n=== 12.3 effective_complexity: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
