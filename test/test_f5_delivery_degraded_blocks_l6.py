#!/usr/bin/env python3
"""F5（round28，正确性 P1 残留）：LEARN_SUCCESS 节点内交付阶段失败仍被学成 L6 成功模式。

两处残留缺口（外部审查坐实）：
  ① in-node 交付 apply/commit 失败只 warn，不进任何 should_write_success 能读的信号；
  ② _degraded（base 不可达）在 persist 之后才并入返回值 → 对成功判据是死信号。
治本：把交付降级（apply 全失败/不完整、commit 失败、base 不可达）在 persist 之前并入
state.degraded_reasons，should_write_success 据 degraded_reasons 非空跳过 L6（C10 已有此门）。

本测试锁【不变量】：这些 delivery 降级信号一旦进 degraded_reasons，即便 complexity=medium、
非部分交付、can_auto_accept=True，也【绝不】写 L6 成功模式（否则失败交付毒化知识库）。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.memory import pattern_extractor

_DELIVERY_DEGRADED = [
    "delivery_apply_failed",
    "delivery_apply_incomplete",
    "delivery_commit_failed",
    "delivery_base_unreachable",
]


def _guard(state):
    # 排除【部分交付】与【真实成功判据】两门，单独验证 degraded_reasons 门对交付降级信号生效。
    with patch("swarm.brain.gates.is_partial_delivery", return_value=False), \
         patch("swarm.brain.gates.can_auto_accept_delivery", return_value=(True, "")):
        return pattern_extractor.should_write_success(state)


def test_control_medium_writes_when_clean():
    """对照：medium + 无降级 + 非部分 + 可自动接受 → 写 L6。"""
    assert _guard({"complexity": "medium"}) is True


def test_each_delivery_degraded_blocks_l6():
    for reason in _DELIVERY_DEGRADED:
        state = {"complexity": "medium", "degraded_reasons": [reason]}
        assert _guard(state) is False, f"{reason} 应阻断 L6 成功模式写入"
    print("  ✅ 每个交付降级信号都跳过 L6（防失败交付毒化）")


def test_delivery_degraded_blocks_even_complex():
    # 高复杂度且其它门全过，交付降级仍须拦（degraded 是独立否决门）。
    state = {"complexity": "complex", "degraded_reasons": ["delivery_commit_failed"]}
    assert _guard(state) is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
