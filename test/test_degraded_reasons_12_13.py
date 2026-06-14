"""audit #12/#13 修复回归测试：analyze/plan LLM 降级时记录 degraded_reasons（标记+告警）。

#12 analyze LLM 失败 → 复杂度静默回退 MEDIUM，应在 degraded_reasons 留痕。
#13 plan LLM 失败 → 空 scope 兜底 plan，应在 degraded_reasons 留痕（不改流转）。

构造态测试：patch _get_brain_llm 抛异常走 except 分支，断言返回 dict 的 degraded_reasons。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.types import Complexity


def _run(coro):
    return asyncio.run(coro)


def test_analyze_degraded_records_reason():
    """analyze LLM 调用失败 → degraded_reasons 含一条，complexity 回退 MEDIUM。"""
    def _boom():
        raise RuntimeError("brain LLM down")

    with patch.object(nodes, "_get_brain_llm", side_effect=_boom):
        state = {
            "task_description": "给用户列表加排序",
            "knowledge_context": {},
            "recent_task_summaries": [],
            "degraded_reasons": [],
        }
        out = _run(nodes.analyze(state))

    assert out["complexity"] == Complexity.MEDIUM
    reasons = out.get("degraded_reasons") or []
    assert any("analyze" in r and "回退 MEDIUM" in r for r in reasons), reasons


def test_plan_degraded_records_reason():
    """plan LLM 调用失败 → degraded_reasons 含一条（空 scope 兜底）。"""
    def _boom():
        raise RuntimeError("brain LLM down")

    with patch.object(nodes, "_get_brain_llm", side_effect=_boom):
        state = {
            "task_description": "实现一个新功能",
            "complexity": Complexity.MEDIUM,  # 非 SIMPLE，走 LLM 路径
            "knowledge_context": {},
            "recent_task_summaries": [],
            "degraded_reasons": [],
        }
        out = _run(nodes.plan(state))

    reasons = out.get("degraded_reasons") or []
    assert any("plan" in r and "兜底" in r for r in reasons), reasons


def test_no_degraded_field_preserved_across_nodes():
    """degraded_reasons 应保留 state 已有条目（多节点累积，不覆盖）。"""
    def _boom():
        raise RuntimeError("down")

    with patch.object(nodes, "_get_brain_llm", side_effect=_boom):
        state = {
            "task_description": "x",
            "complexity": Complexity.MEDIUM,
            "knowledge_context": {},
            "recent_task_summaries": [],
            "degraded_reasons": ["前序节点已有的降级"],
        }
        out = _run(nodes.plan(state))

    reasons = out.get("degraded_reasons") or []
    assert "前序节点已有的降级" in reasons, "不应覆盖已有降级条目"
    assert len(reasons) >= 2


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
    print(f"\n=== #12/#13 degraded visibility: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
