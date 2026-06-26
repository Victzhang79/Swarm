#!/usr/bin/env python3
"""Wave 1 LLM 响应 schema 边界测试（TD2606-B1）。

钉死「载荷关键字段严格类型、装饰性字段容忍、坏形状显式失败（不静默错形）」：
  - complexity 非法形状（list/未知值）→ ValidationError；合法 → 枚举。
  - stack confidence 强制 float；frontend 缺失 → 失败。
  - failure strategy 仅限已知集合，未知 → 失败（调用方回退 retry）。
  - file_plan 丢弃无 path 的 malformed 项。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_complexity_valid_and_bad_shape():
    from swarm.brain.llm_schemas import ComplexityAssessmentResponse
    from swarm.types import Complexity

    # 合法（大小写/装饰字段容忍）
    m = ComplexityAssessmentResponse.model_validate(
        {"complexity": "ULTRA", "reasoning": "x", "key_risks": "single risk"})
    assert m.complexity is Complexity.ULTRA
    assert m.key_risks == ["single risk"]  # 字符串容忍为单元素列表

    # 非法形状（list）→ 显式失败
    for bad in ({"complexity": ["ultra"]}, {"complexity": "moderate"}, {}):
        try:
            ComplexityAssessmentResponse.model_validate(bad)
            raise AssertionError(f"应拒绝坏形状: {bad}")
        except AssertionError:
            raise
        except Exception:
            pass
    print("  ✅ complexity 严格枚举 + 装饰字段容忍")


def test_stack_confidence_coerce_and_frontend_required():
    from swarm.brain.llm_schemas import StackAdjudicateResponse

    m = StackAdjudicateResponse.model_validate({"frontend": "vue", "confidence": "0.8"})
    assert m.confidence == 0.8  # 字符串强制 float
    m2 = StackAdjudicateResponse.model_validate({"frontend": "react", "confidence": "garbage"})
    assert m2.confidence == 0.5  # 非数值 → 默认
    try:
        StackAdjudicateResponse.model_validate({"confidence": 0.9})  # 缺 frontend
        raise AssertionError("缺 frontend 应失败")
    except AssertionError:
        raise
    except Exception:
        pass
    print("  ✅ stack confidence 强制 float + frontend 必填")


def test_failure_strategy_known_only():
    from swarm.brain.llm_schemas import FailureStrategyResponse

    assert FailureStrategyResponse.model_validate({"strategy": "Replan"}).strategy == "replan"
    for bad in ("nuke", "", "retry_now", 123):
        try:
            FailureStrategyResponse.model_validate({"strategy": bad})
            raise AssertionError(f"未知策略应失败: {bad}")
        except AssertionError:
            raise
        except Exception:
            pass
    print("  ✅ failure strategy 仅限已知集合")


def test_validate_file_plan_drops_malformed():
    from swarm.brain.llm_schemas import validate_file_plan

    items = [{"path": "a.py"}, {"desc": "no path"}, {"path": ""}, "garbage", {"path": "b.py", "module": "x"}]
    kept = validate_file_plan(items, module="m")
    assert [k["path"] for k in kept] == ["a.py", "b.py"]
    assert kept[0]["module"] == "m"   # 补全缺失 module
    assert kept[1]["module"] == "x"   # 保留已有 module
    assert validate_file_plan("not a list") == []
    print("  ✅ file_plan 丢弃无 path 的 malformed 项")


def test_parse_and_validate_raises_on_bad_json():
    from swarm.brain.llm_schemas import ComplexityAssessmentResponse
    from swarm.brain.nodes.shared import parse_and_validate

    # 合法 JSON + 合法形状
    m = parse_and_validate('{"complexity": "complex"}', ComplexityAssessmentResponse)
    assert m.complexity.value == "complex"
    # 合法 JSON 但坏形状 → 抛出（调用方据此显式降级）
    try:
        parse_and_validate('{"complexity": 42}', ComplexityAssessmentResponse)
        raise AssertionError("坏形状应抛出")
    except AssertionError:
        raise
    except Exception:
        pass
    print("  ✅ parse_and_validate 坏形状抛出（不静默错形）")


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {fn.__name__}: {e}")
            fails += 1
    sys.exit(1 if fails else 0)
