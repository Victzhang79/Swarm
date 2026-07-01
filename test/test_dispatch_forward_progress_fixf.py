#!/usr/bin/env python3
"""Fix F·dispatch 前进保证（解 head-of-line 死锁）单测 — 固化 get_dispatch_batch 的
【失败重试子任务降优先级】行为，防回归。

背景（round15 死因）：失败的一小撮子任务（早序、恒就绪、常撞 900s worker 超时）在旧
`ready[:max_concurrent]` 下每批霸占并发槽 → 从未尝试的就绪生产者（新前沿）被饿死 →
完成数冻结 → 15 轮无一到 MERGE。Fix F 让 dispatch 把重试撮（deprioritized）降到 fresh
之后，纯优先级重排（非丢弃）。

判据（对齐 UPSTREAM_DOWNSTREAM_TREATMENT.md §3 Fix F 测试）：
- 有失败重试撮 + 若干就绪生产者时，dispatch 批【包含】生产者（非只失败撮）；
- 失败撮仍可派发（在 remaining、槽有余时仍进批）→ 不破坏有界重试；
- deprioritized 为空 → 完全等价旧行为（向后兼容）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _sub(sid, deps=None):
    return SubTask(
        id=sid, description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[], readable=[]), depends_on=deps or [],
    )


def test_failing_retry_does_not_starve_fresh_producers():
    """核心 head-of-line 场景：2 个失败重试撮（早序、就绪）+ 4 个就绪生产者，
    max_concurrent=2 → 旧逻辑批=失败撮，生产者饿死。Fix F 后批=生产者。"""
    # st-1/st-2 早序且在 deprioritized（正重试）；st-3..st-6 是从未尝试的生产者
    plan = TaskPlan(subtasks=[_sub(f"st-{i}") for i in range(1, 7)])
    remaining = [f"st-{i}" for i in range(1, 7)]
    batch = plan.get_dispatch_batch(
        set(), remaining, max_concurrent=2,
        deprioritized={"st-1", "st-2"},
    )
    ids = {t.id for t in batch}
    assert ids == {"st-3", "st-4"}, f"生产者应优先占槽（非失败撮），实际 {ids}"
    # 失败撮未被丢弃 —— 仍在 remaining，只是本批未占槽
    assert "st-1" in remaining and "st-2" in remaining
    print("  ✅ 失败重试撮不再霸占并发槽 → 生产者（新前沿）优先派发")


def test_retry_fills_leftover_slots_not_starved_forever():
    """槽有余时失败撮仍进批：2 个生产者 + 2 个失败重试撮，max_concurrent=4 →
    批含全部 4 个（fresh 先、retry 填余槽），失败撮不被永久饿死 → 有界重试完好。"""
    plan = TaskPlan(subtasks=[_sub("st-1"), _sub("st-2"), _sub("st-3"), _sub("st-4")])
    batch = plan.get_dispatch_batch(
        set(), ["st-1", "st-2", "st-3", "st-4"], max_concurrent=4,
        deprioritized={"st-1", "st-2"},
    )
    ids = [t.id for t in batch]
    assert set(ids) == {"st-1", "st-2", "st-3", "st-4"}, f"槽有余应含失败撮，实际 {ids}"
    # fresh（st-3/st-4）排在 retry（st-1/st-2）之前
    assert ids.index("st-3") < ids.index("st-1"), "fresh 应排在 retry 之前"
    assert ids.index("st-4") < ids.index("st-2"), "fresh 应排在 retry 之前"
    print("  ✅ 槽有余时失败撮填剩余槽（非丢弃/非永久饿死），有界重试完好")


def test_frontier_advances_after_producers_merge():
    """生产者合并后，之前被 depends_on 阻塞的下游成为新 fresh 前沿，优先于旧失败撮。"""
    # st-fail 恒就绪且在重试；st-prod 是生产者；st-down 依赖 st-prod
    plan = TaskPlan(subtasks=[
        _sub("st-fail"), _sub("st-prod"), _sub("st-down", deps=["st-prod"]),
    ])
    # 第一批：st-prod（fresh）优先于 st-fail（retry），st-down 仍被依赖阻塞
    b1 = plan.get_dispatch_batch(
        set(), ["st-fail", "st-prod", "st-down"], max_concurrent=1,
        deprioritized={"st-fail"},
    )
    assert {t.id for t in b1} == {"st-prod"}, f"生产者应先跑，实际 {[t.id for t in b1]}"
    # st-prod 完成 → st-down 成为 fresh 前沿，仍优先于 st-fail 重试撮
    b2 = plan.get_dispatch_batch(
        {"st-prod"}, ["st-fail", "st-down"], max_concurrent=1,
        deprioritized={"st-fail"},
    )
    assert {t.id for t in b2} == {"st-down"}, f"新前沿应先于旧失败撮，实际 {[t.id for t in b2]}"
    print("  ✅ 生产者合并 → 新前沿推进，持续优先于失败重试撮（前进保证）")


def test_no_deprioritized_equals_legacy_behavior():
    """deprioritized 为空/None → 完全等价旧行为（向后兼容，不改无失败时的调度）。"""
    plan = TaskPlan(subtasks=[_sub(f"st-{i}") for i in range(1, 6)])
    remaining = [f"st-{i}" for i in range(1, 6)]
    legacy = plan.get_dispatch_batch(set(), remaining, max_concurrent=3)
    with_none = plan.get_dispatch_batch(set(), remaining, max_concurrent=3, deprioritized=None)
    with_empty = plan.get_dispatch_batch(set(), remaining, max_concurrent=3, deprioritized=set())
    assert [t.id for t in legacy] == [t.id for t in with_none] == [t.id for t in with_empty]
    assert [t.id for t in legacy] == ["st-1", "st-2", "st-3"], "无重试撮时按原序截断"
    print("  ✅ deprioritized 空/None 等价旧行为（向后兼容）")


def test_stable_order_within_groups():
    """两组各自保持 self.subtasks 稳定序 → 确定性（无随机/无抖动）。"""
    plan = TaskPlan(subtasks=[_sub(f"st-{i}") for i in range(1, 9)])
    remaining = [f"st-{i}" for i in range(1, 9)]
    # st-2, st-5 在重试 → 应被压到末尾，其余保持原序
    batch = plan.get_dispatch_batch(
        set(), remaining, max_concurrent=8,
        deprioritized={"st-2", "st-5"},
    )
    ids = [t.id for t in batch]
    assert ids == ["st-1", "st-3", "st-4", "st-6", "st-7", "st-8", "st-2", "st-5"], ids
    print("  ✅ 组内稳定序保持 → 派发确定性")


if __name__ == "__main__":
    test_failing_retry_does_not_starve_fresh_producers()
    test_retry_fills_leftover_slots_not_starved_forever()
    test_frontier_advances_after_producers_merge()
    test_no_deprioritized_equals_legacy_behavior()
    test_stable_order_within_groups()
    print("\n✅ Fix F 全部通过")
