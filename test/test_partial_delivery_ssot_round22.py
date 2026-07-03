#!/usr/bin/env python3
"""#3 round22：PARTIAL 部分交付单一事实源（复现 give_up 假成功学习）。

根因：终态 PARTIAL = abandoned ∪ give_up（runner.py:611-614），但 learn 侧只看 abandoned：
- learn_store.py:103 `outcome = "partial" if abandoned else "success"`
- pattern_extractor.py:19 `if abandoned: return False`
→ give_up-only 的 PARTIAL 穿透 gate+L2 outcome+L6 门槛，被学成"可复用成功模式"（自毒化）。

治本：单一事实源 `is_partial_delivery(state)=abandoned ∪ give_up`（gates.py），learn 侧改看并集；
TaskStatus.is_terminal_status 收口终态集（含 PARTIAL）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.gates import is_partial_delivery, partial_delivery_ids  # noqa: E402
from swarm.memory.pattern_extractor import should_write_success  # noqa: E402
from swarm.types import Complexity, TaskStatus  # noqa: E402


# ── is_partial_delivery / partial_delivery_ids 单一事实源 ──

def test_give_up_only_is_partial():
    st = {"give_up_isolated_ids": ["s3"], "abandoned_subtask_ids": []}
    assert is_partial_delivery(st) is True
    assert partial_delivery_ids(st) == ["s3"]
    print("  ✅ give_up-only → partial")


def test_abandoned_only_is_partial():
    st = {"give_up_isolated_ids": [], "abandoned_subtask_ids": ["s1"]}
    assert is_partial_delivery(st) is True
    print("  ✅ abandoned-only → partial")


def test_union_dedup():
    st = {"give_up_isolated_ids": ["s3", "s1"], "abandoned_subtask_ids": ["s1"]}
    assert partial_delivery_ids(st) == ["s1", "s3"]
    print("  ✅ 并集去重保序")


def test_neither_not_partial():
    assert is_partial_delivery({}) is False
    print("  ✅ 无放弃 → 非 partial")


# ── 核心：give_up-only PARTIAL 绝不学成成功模式 ──

def _would_be_success_state():
    """构造一个除 give_up 外满足所有成功判据的 state。"""
    return {
        "give_up_isolated_ids": ["s3"],
        "abandoned_subtask_ids": [],
        "failed_subtask_ids": [],
        "l2_passed": True,
        "complexity": Complexity.MEDIUM,
    }


def test_give_up_partial_not_learned_success():
    st = _would_be_success_state()
    assert should_write_success(st) is False, "give_up-only PARTIAL 绝不能学成 L6 成功模式（复现 bug：当前 True）"
    print("  ✅ give_up-only PARTIAL → should_write_success=False")


def test_clean_success_still_learned():
    """不回归：真正的干净成功仍学成功模式。"""
    st = {"give_up_isolated_ids": [], "abandoned_subtask_ids": [], "failed_subtask_ids": [],
          "l2_passed": True, "complexity": Complexity.MEDIUM}
    assert should_write_success(st) is True
    print("  ✅ 干净成功 → should_write_success=True（不回归）")


# ── TaskStatus 终态集 SSOT ──

def test_taskstatus_terminal_includes_partial():
    assert TaskStatus.is_terminal_status("PARTIAL") is True
    assert TaskStatus.is_terminal_status("DONE") is True
    assert TaskStatus.is_terminal_status(TaskStatus.FAILED) is True
    assert TaskStatus.is_terminal_status("MONITORING") is False
    print("  ✅ TaskStatus.is_terminal_status 含 PARTIAL")


def test_taskstatus_successful_only_done():
    assert TaskStatus.is_successful_status("DONE") is True
    assert TaskStatus.is_successful_status("PARTIAL") is False
    print("  ✅ is_successful_status 仅 DONE")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\n✅ #3 PARTIAL 单一事实源全部通过")
