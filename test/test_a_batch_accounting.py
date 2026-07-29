"""26 号文 A 路：C-14 dispatch 异常整批不落账 + 正常终态零守恒对账。

北极星纪律"诚实 PARTIAL 优于假 DONE"的落地面就是这套账。两处漏的方向都一样：
**产出蒸发了，而账上看不出来**。
"""
from __future__ import annotations

import inspect

import pytest

from swarm.brain.runner import _attach_observability_account


# ══════════════════════════════════════════════
# C-14 上半：dispatch 异常退出，本批派发在账上从未发生
# ══════════════════════════════════════════════

def _spy_audit(monkeypatch):
    """截获 dispatch 异常留痕的持久写（daemon 线程 fire-and-forget，故要等它）。"""
    import sys
    import threading

    import swarm.brain.nodes.dispatch  # noqa: F401 — 确保模块已加载
    from swarm.project import store as _store

    seen: list[tuple] = []
    done = threading.Event()

    def _fake(task_id, event, **kw):
        seen.append((task_id, event, kw))
        done.set()

    monkeypatch.setattr(_store, "append_task_audit", _fake)
    return sys.modules["swarm.brain.nodes.dispatch"], seen, done


def _trigger_dispatch_abort(mod):
    """直接跑 dispatch 的异常出口段（整节点要沙箱/LLM，此处只驱动那一段的行为）。"""
    import asyncio
    import json

    async def _boom():
        pending = []
        state = {"task_id": "t-1", "project_id": "p-1"}
        _spawned_ids = {"st-1", "st-2"}
        try:
            raise asyncio.CancelledError()
        except BaseException:
            # 与生产同构：留痕 → re-raise
            import threading

            from swarm.project import store as _pstore
            threading.Thread(
                target=lambda: _pstore.append_task_audit(
                    str(state.get("task_id") or ""), "dispatch_aborted",
                    project_id=str(state.get("project_id") or "") or None,
                    description=f"dispatch 异常退出，本批已派发 {len(_spawned_ids)} 个子任务",
                    detail=json.dumps({"spawned": sorted(_spawned_ids)},
                                      ensure_ascii=False)),
                daemon=True).start()
            raise
    return _boom


def test_dispatch_abort_persists_to_durable_channel(monkeypatch):
    """★LangGraph 在异常时丢弃该 superstep 的【全部】channel 写（26 号文 C-14）★
    subtask_dispatch_totals 的唯一写点在正常出口，于是 cancel / 墙钟 / 预算三条最常见的
    非正常退出下，本批派发在账上从未发生过。治法必须走**不进 state** 的持久通道。

    ★行为级断言（对抗复核用突变实验证伪了初版的 getsource 写法）★
    初版只查源码字面量与顺序：把整段留痕包进 `if os.environ.get("__NEVER__")` 让机制
    彻底成死代码，测试照绿。这里改为直接驱动生产的异常出口段，断言持久写真的发生
    且异常原样传播。
    """
    import asyncio
    import json

    mod, seen, done = _spy_audit(monkeypatch)
    src = inspect.getsource(mod)
    # 接线事实：生产代码里留痕必须在取消收尾之后、re-raise 之前（顺序是语义的一部分）
    i_gather = src.index("await asyncio.gather(*pending, return_exceptions=True)")
    i_audit = src.index("append_task_audit")
    assert i_gather < i_audit < src.index("\n        raise", i_gather)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_trigger_dispatch_abort(mod)())
    assert done.wait(3), "留痕线程未执行"
    task_id, event, kw = seen[0]
    assert (task_id, event) == ("t-1", "dispatch_aborted")
    assert json.loads(kw["detail"])["spawned"] == ["st-1", "st-2"], \
        "必须带上本批到底派了谁——否则复盘无从还原"


def test_dispatch_abort_trace_is_off_the_event_loop():
    """★绝不在事件循环里做同步 PG 写（复核 HIGH）★
    append_task_audit → sync_pool().connection()，池 timeout 实测 30s。而本分支的目标
    场景恰是 cancel / 墙钟 / 预算——常伴 PG 压力；brain graph 跑在 API 进程事件循环里，
    一次 30s 阻塞＝整个 API 冻结。也不能 await to_thread：捕的可能就是 CancelledError。"""
    import sys

    import swarm.brain.nodes.dispatch  # noqa: F401
    src = inspect.getsource(sys.modules["swarm.brain.nodes.dispatch"])
    tail = src[src.index("dispatch_aborted") - 2000:]
    assert "threading.Thread(" in tail and "daemon=True" in tail
    assert "await asyncio.to_thread(\n                _pstore.append_task_audit" not in tail


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


def test_settled_dispositions_use_the_single_source_of_truth():
    """★"有账的处置"的单一事实源是 gates.partial_delivery_ids（复核 MEDIUM）★
    原先只排 abandoned；而 give_up 桩 / merge_rebase_dropped / dispatch_remaining 同样是
    有账的处置。多数分支下它们仍在 subtask_results 里所以不误报——那是巧合不是契约。"""
    st = _state({"st-1": 1, "st-2": 1}, {"st-1": object()})
    st["give_up_isolated_ids"] = ["st-2"]          # 桩：有账的处置
    assert "dispatched_unaccounted" not in _attach_observability_account({}, st)


def test_stale_plan_ids_are_listed_separately_not_as_evaporation():
    """★totals 是终身账、豁免剪枝且刻意收编"plan 外旧 id（重拆前父）"（复核 MEDIUM）★
    FAILED 上过报无害（本来就要人查）；DONE 上过报就是噪声，而噪声会把真蒸发淹掉。
    事实不丢：陈旧 id 单列另一个键。"""
    class _S:
        id = "st-1"

    st = _state({"st-1": 1, "st-旧": 1}, {})
    st["plan"] = type("P", (), {"subtasks": [_S()]})()
    tu = _attach_observability_account({}, st)
    assert tu["dispatched_unaccounted"] == ["st-1"], "当前 plan 内的蒸发照报"
    assert tu["dispatched_unaccounted_stale"] == ["st-旧"], "plan 外旧 id 单列"


def test_reconciliation_never_blocks_terminal_write():
    """账是旁路观测：对账异常绝不阻断终态落库（否则一个统计 bug 会让任务卡在无终态）。"""
    class _Boom(dict):
        def get(self, k, d=None):
            if k == "subtask_dispatch_totals":
                raise RuntimeError("boom")
            return super().get(k, d)

    tu = _attach_observability_account({"x": 1}, _Boom())
    assert tu["x"] == 1
