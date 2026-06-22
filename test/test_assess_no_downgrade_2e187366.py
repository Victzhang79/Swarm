"""治本 task 2e187366：ASSESS 澄清后定级不得把 complexity 下调到 analyze 初判之下。

现场：ANALYZE 正确判 ultra(企业级全栈多模块平台)，ASSESS 据澄清把它降到 complex →
complex 的 tech_design 更浅 → 只产 27 文件(对照 auto 同 PRD 出 98 文件)，全栈需求做不完。
修复：ASSESS 可【上调】(纠正低估)，但不得【下调】到 analyze 之下，取两者较高档。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.planning_nodes as pn
from swarm.types import Complexity


class _Resp:
    def __init__(self, content): self.content = content


def _llm_grading(grade):
    class _L:
        async def ainvoke(self, _msgs):
            return _Resp('{"complexity":"%s","reason":"x","needs_tech_design":true}' % grade)
    return lambda: _L()


def _run(state):
    return asyncio.run(pn.assess(state))


def test_assess_does_not_downgrade_ultra_to_complex():
    """analyze=ultra(枚举)，ASSESS 给 complex → 守住 ultra。"""
    with patch.object(pn, "_get_brain_llm", _llm_grading("complex")):
        out = _run({"complexity": Complexity.ULTRA, "task_description": "企业级预警平台", "clarify_summary": "x"})
    assert out["complexity"] == Complexity.ULTRA, out
    assert out["assessed_complexity"] == Complexity.ULTRA


def test_assess_no_downgrade_when_complexity_is_STRING():
    """治本 308cd191：checkpoint resume 后 complexity 是字符串 'ultra'（非枚举），守卫仍须生效。"""
    with patch.object(pn, "_get_brain_llm", _llm_grading("complex")):
        out = _run({"complexity": "ultra", "task_description": "企业级预警平台", "clarify_summary": "x"})
    assert out["complexity"] == Complexity.ULTRA, out  # 字符串也要守住 ultra


def test_assess_no_downgrade_string_complex_over_medium_llm():
    """analyze 字符串 'complex'，ASSESS 给 medium → 守住 complex。"""
    with patch.object(pn, "_get_brain_llm", _llm_grading("medium")):
        out = _run({"complexity": "complex", "task_description": "x", "clarify_summary": "x"})
    assert out["complexity"] == Complexity.COMPLEX, out


def test_assess_can_still_upgrade():
    """analyze=medium，ASSESS 给 ultra → 上调到 ultra（纠正低估仍允许）。"""
    with patch.object(pn, "_get_brain_llm", _llm_grading("ultra")):
        out = _run({"complexity": Complexity.MEDIUM, "task_description": "x", "clarify_summary": "x"})
    assert out["complexity"] == Complexity.ULTRA, out


def test_assess_keeps_equal_grade():
    """analyze=complex，ASSESS 给 complex → 不变。"""
    with patch.object(pn, "_get_brain_llm", _llm_grading("complex")):
        out = _run({"complexity": Complexity.COMPLEX, "task_description": "x", "clarify_summary": "x"})
    assert out["complexity"] == Complexity.COMPLEX, out


def test_assess_downgrade_within_allowed_when_analyze_lower():
    """analyze=medium，ASSESS 给 complex（上调）→ complex；不存在下调问题。"""
    with patch.object(pn, "_get_brain_llm", _llm_grading("complex")):
        out = _run({"complexity": Complexity.MEDIUM, "task_description": "x", "clarify_summary": "x"})
    assert out["complexity"] == Complexity.COMPLEX, out


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
