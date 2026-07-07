#!/usr/bin/env python3
"""D25 治本单测 —— 终态账本纳入 dispatch_remaining：悬空依赖滞留 → PARTIAL 非假 DONE。

旧 bug：悬空依赖/不可派发子任务经 #R13-4 熔断进 MERGE，但 partial_delivery_ids 只并
abandoned∪give_up∪rebase_dropped，不看 dispatch_remaining → abandoned/give_up 全空时终态判 DONE，
N 个从未执行子任务被静默吞掉、还被 LEARN_SUCCESS 学成成功。
治本：partial_delivery_ids 纳入 dispatch_remaining（该函数只在终态被 runner 落库/learn 消费，
正常 DONE 时 remaining 已排空）→ 有滞留即 PARTIAL。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.gates import is_partial_delivery, partial_delivery_ids  # noqa: E402


def test_lingering_remaining_is_partial():
    st = {
        "abandoned_subtask_ids": [],
        "give_up_isolated_ids": [],
        "merge_rebase_dropped": [],
        "dispatch_remaining": ["st-down"],  # 悬空依赖滞留、从未执行
    }
    assert is_partial_delivery(st) is True, "有滞留未执行子任务 → PARTIAL(非 DONE)"
    assert partial_delivery_ids(st) == ["st-down"]
    print("  ✅ dispatch_remaining 非空 → PARTIAL(悬空依赖不静默 DONE)")


def test_empty_remaining_still_done():
    # 正常完成：remaining 排空、无放弃 → 仍 DONE（不误报 PARTIAL）。
    st = {
        "abandoned_subtask_ids": [],
        "give_up_isolated_ids": [],
        "merge_rebase_dropped": [],
        "dispatch_remaining": [],
    }
    assert is_partial_delivery(st) is False
    assert partial_delivery_ids(st) == []
    print("  ✅ remaining 排空 + 无放弃 → 仍 DONE")


def test_union_with_other_partial_sources_dedup():
    st = {
        "abandoned_subtask_ids": ["st-a"],
        "give_up_isolated_ids": ["st-b"],
        "merge_rebase_dropped": [],
        "dispatch_remaining": ["st-b", "st-c"],  # st-b 与 give_up 重叠
    }
    assert partial_delivery_ids(st) == ["st-a", "st-b", "st-c"], "四来源并集去重保序"
    print("  ✅ 与 abandoned/give_up 并集去重")


def test_missing_keys_default_empty():
    assert partial_delivery_ids({}) == []
    assert is_partial_delivery({}) is False
    print("  ✅ 缺键默认空 → 非 PARTIAL")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("D25 全部通过")
