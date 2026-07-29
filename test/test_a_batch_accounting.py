"""26 号文 A 路：C-14 dispatch 异常整批不落账 + 正常终态零守恒对账。

北极星纪律"诚实 PARTIAL 优于假 DONE"的落地面就是这套账。两处漏的方向都一样：
**产出蒸发了，而账上看不出来**。
"""
from __future__ import annotations

import inspect

from swarm.brain.runner import _attach_observability_account


# ══════════════════════════════════════════════
# C-14 上半：dispatch 异常退出，本批派发在账上从未发生
# ══════════════════════════════════════════════

def test_dispatch_abort_persists_to_durable_channel():
    """★LangGraph 在异常时丢弃该 superstep 的【全部】channel 写（26 号文 C-14）★
    subtask_dispatch_totals 的唯一写点在正常出口，于是 cancel / 墙钟 / 预算三条最常见的
    非正常退出下，本批派发在账上从未发生过。而 runner 的两个消费者都以该表为唯一驱动：
    幽灵件清扫 + 终态账务守恒。
    治法必须走**不进 state** 的持久通道——否则改多少行都会被同一次 superstep 丢弃一起带走。
    """
    # `swarm.brain.nodes.dispatch` 这个名字在包里被 re-export 成了节点函数本身，
    # 不是模块——取模块要走 sys.modules（否则 getsource 拿到的是别的东西）。
    import sys
    import swarm.brain.nodes.dispatch  # noqa: F401 — 确保模块已加载
    src = inspect.getsource(sys.modules["swarm.brain.nodes.dispatch"])
    i_gather = src.index("await asyncio.gather(*pending, return_exceptions=True)")
    i_audit = src.index('append_task_audit')
    i_raise = src.index("\n        raise", i_gather)
    assert i_gather < i_audit < i_raise, "留痕必须在取消收尾之后、re-raise 之前"
    assert "dispatch_aborted" in src, "审计事件名是复盘的 grep 锚点"


def test_dispatch_abort_trace_never_swallows_the_exception():
    """★留痕绝不改变异常传播★：异常必须原样抛出去（上层据它判 salvage/终态），
    留痕失败也只吞自己（append_task_audit 本身即 best-effort）。"""
    # `swarm.brain.nodes.dispatch` 这个名字在包里被 re-export 成了节点函数本身，
    # 不是模块——取模块要走 sys.modules（否则 getsource 拿到的是别的东西）。
    import sys
    import swarm.brain.nodes.dispatch  # noqa: F401 — 确保模块已加载
    src = inspect.getsource(sys.modules["swarm.brain.nodes.dispatch"])
    tail = src[src.index("dispatch_aborted"):]
    assert "except Exception:" in tail and "raise" in tail


# ══════════════════════════════════════════════
# C-14 下半：守恒对账只在 FAILED 专路，DONE/PARTIAL 全盲
# ══════════════════════════════════════════════

def _state(totals, results, abandoned=()):
    return {
        "task_id": "",                       # 空 → 跳过 ledger 快照（本用例不测那条）
        "subtask_dispatch_totals": totals,
        "subtask_results": results,
        "abandoned_subtask_ids": list(abandoned),
    }


def test_evaporated_subtask_is_caught_on_normal_terminal():
    """★DONE 恰恰是最需要这条守恒的终态（26 号文 C-14）★
    原先 dispatched_unaccounted 只在 _failed_machine_account 里算——那是 FAILED 专路。
    FAILED 本来就会被人查；一个静默丢了子任务的 DONE 不会。"""
    tu = _attach_observability_account(
        {}, _state({"st-1": 1, "st-2": 1}, {"st-1": object()}))
    assert tu["dispatched_unaccounted"] == ["st-2"]


def test_abandoned_is_not_evaporated():
    """放弃是**有账的**处置（终态已诚实 PARTIAL），不是蒸发——绝不能报进守恒缺口，
    否则真蒸发被噪声淹掉。"""
    tu = _attach_observability_account(
        {}, _state({"st-1": 1, "st-2": 1}, {"st-1": object()}, abandoned=["st-2"]))
    assert "dispatched_unaccounted" not in tu


def test_all_accounted_produces_no_key():
    """全平账 → 一个键都不加（绝大多数轮），零噪声。"""
    tu = _attach_observability_account(
        {}, _state({"st-1": 1}, {"st-1": object()}))
    assert "dispatched_unaccounted" not in tu


def test_failed_path_value_is_not_overwritten():
    """FAILED 路径先算过就不覆写——两条路的判据逐字同源，值本应相同；
    但"先填不覆写"是本仓既有约定（见 _attach_observability_account 的 ledger 合并），
    保持一致比抢写安全。"""
    tu = _attach_observability_account(
        {"dispatched_unaccounted": ["先填的"]},
        _state({"st-1": 1, "st-2": 1}, {"st-1": object()}))
    assert tu["dispatched_unaccounted"] == ["先填的"]


def test_reconciliation_uses_one_ruler():
    """★口径同源★：两条路的判据必须是同一把尺子（totals − results − abandoned）。
    两份"哪些算蒸发"的定义漂移，正是本仓反复栽跟头的形态。"""
    from swarm.brain import runner as _r
    common = inspect.getsource(_r._attach_observability_account)
    failed = inspect.getsource(_r._failed_machine_account)
    for src in (common, failed):
        assert "subtask_dispatch_totals" in src
        assert "abandoned_subtask_ids" in src


def test_reconciliation_never_blocks_terminal_write():
    """账是旁路观测：对账异常绝不阻断终态落库（否则一个统计 bug 会让任务卡在无终态）。"""
    class _Boom(dict):
        def get(self, k, d=None):
            if k == "subtask_dispatch_totals":
                raise RuntimeError("boom")
            return super().get(k, d)

    tu = _attach_observability_account({"x": 1}, _Boom())
    assert tu["x"] == 1
