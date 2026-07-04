"""B10（round22, P2）：worker_parallel_pool 轮转覆盖 difficulty 路由，complex 子任务落弱模型。

根因：池非空时 trivial/medium/complex 子任务均按 idx 轮转 pool 内模型；complex 子任务可能被
分到 pool 中较弱模型(如 MiniMax vs 40B)先降质跑一轮，再靠 force_strong 补救（已有安全阀但多烧一轮）。

治本（最小）：complex/ultra 子任务首派即绕过 pool 轮转直取 routing_complex。medium/trivial 仍轮转。
"""
from __future__ import annotations

from swarm.brain.nodes.dispatch import _select_pool_override
from swarm.types import SubTaskDifficulty

_POOL = ["40B", "MiniMax"]
_STRONG = "routing-complex-40B"


def test_complex_bypasses_pool_to_strongest():
    # idx=1 本会轮到 MiniMax，但 complex 应直取最强
    got = _select_pool_override(SubTaskDifficulty.COMPLEX, 1, _POOL, False, False, _STRONG)
    assert got == _STRONG, got


def test_medium_uses_pool_roundrobin():
    assert _select_pool_override(SubTaskDifficulty.MEDIUM, 0, _POOL, False, False, _STRONG) == "40B"
    assert _select_pool_override(SubTaskDifficulty.MEDIUM, 1, _POOL, False, False, _STRONG) == "MiniMax"


def test_trivial_uses_pool_roundrobin():
    assert _select_pool_override(SubTaskDifficulty.TRIVIAL, 1, _POOL, False, False, _STRONG) == "MiniMax"


def test_force_strong_always_strongest():
    got = _select_pool_override(SubTaskDifficulty.TRIVIAL, 0, _POOL, False, True, _STRONG)
    assert got == _STRONG


def test_alternate_no_override():
    # use_alternate 生效时不轮转（走 difficulty 路由兜底）
    assert _select_pool_override(SubTaskDifficulty.MEDIUM, 0, _POOL, True, False, _STRONG) is None


def test_empty_pool_no_override():
    assert _select_pool_override(SubTaskDifficulty.MEDIUM, 0, [], False, False, _STRONG) is None


def test_string_difficulty_also_handled():
    assert _select_pool_override("complex", 0, _POOL, False, False, _STRONG) == _STRONG


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
