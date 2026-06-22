"""治本 task 8537fa5e：checkpoint resume 后 complexity 反序列化成字符串。

现场：ultra 任务经 review_design interrupt → resume → plan() 节点
`complexity.value` 抛 AttributeError('str' object has no attribute 'value')，
规划 resume 失败 → 任务 FAILED。与 ASSESS 308cd191 同类（interrupt/resume 路径枚举退化为 str）。

修复：① effective_complexity（唯一真值入口）统一归一为 Complexity 枚举；
② plan() 绑定 complexity 后显式归一。本测守住两者对字符串输入的健壮性。
"""
from __future__ import annotations

from swarm.brain.state import effective_complexity
from swarm.types import Complexity


def test_effective_complexity_coerces_string_ultra():
    assert effective_complexity({"complexity": "ultra"}) == Complexity.ULTRA


def test_effective_complexity_coerces_assessed_string_over_complexity():
    # assessed_complexity 优先，字符串也要归一
    out = effective_complexity({"complexity": "medium", "assessed_complexity": "ultra"})
    assert out == Complexity.ULTRA
    assert isinstance(out, Complexity)


def test_effective_complexity_passthrough_enum():
    assert effective_complexity({"assessed_complexity": Complexity.COMPLEX}) == Complexity.COMPLEX


def test_effective_complexity_garbage_falls_back_medium():
    assert effective_complexity({"complexity": "nonsense"}) == Complexity.MEDIUM
    assert effective_complexity({}) == Complexity.MEDIUM


def test_effective_complexity_result_has_value_attr():
    # 下游会 .value —— 归一后必须是枚举（有 .value）
    out = effective_complexity({"complexity": "ultra"})
    assert out.value == "ultra"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
