"""26 号文 A 路：C-14 dispatch 异常整批不落账 + 正常终态零守恒对账。

北极星纪律"诚实 PARTIAL 优于假 DONE"的落地面就是这套账。两处漏的方向都一样：
**产出蒸发了，而账上看不出来**。
"""
from __future__ import annotations

import pytest

from swarm.brain.runner import _attach_observability_account


# ══════════════════════════════════════════════
# C-14 上半：dispatch 异常退出，本批派发在账上从未发生
# ══════════════════════════════════════════════

def _spy_audit(monkeypatch, block_event=None):
    """截获 dispatch 异常留痕的持久写（daemon 线程 fire-and-forget，故要等它）。

    block_event 给定时，桩先卡在该事件上再记账——用于构造「PG 写阻塞」现场，
    验证留痕绝不阻塞事件循环上的异常传播（保险丝 10s，故障形态下绝不真死锁）。"""
    import sys
    import threading

    import swarm.brain.nodes.dispatch  # noqa: F401 — 确保模块已加载
    from swarm.project import store as _store

    seen: list[tuple] = []
    done = threading.Event()

    def _fake(task_id, event, **kw):
        if block_event is not None:
            block_event.wait(10)
        seen.append((task_id, event, kw))
        done.set()

    monkeypatch.setattr(_store, "append_task_audit", _fake)
    return sys.modules["swarm.brain.nodes.dispatch"], seen, done


def _drive_dispatch_abort(monkeypatch, block_event=None):
    """批25 GS-5w 换锁：真驱动生产 dispatch 节点走 C-14 异常出口，绝不复刻出口段。

    场景构造：st-a 的 worker 抛 TaskTokenLimitExceeded（_run_one 对其原样上抛，
    H2 语义）→ while 循环 `_fut.result()` 穿透 → 进 except BaseException 出口；
    st-b 挂在可取消的 sleep 上——它在事件循环上观察到 CancelledError 的时刻，
    就是 except 块里「取消兄弟 + await gather 收尾」真的完成的时刻
    （except 块内从 cancel 到留痕是纯同步代码，没有 gather 的 await 让出，
    事件循环绝不会先调度 st-b 的取消）→ 序由此可机读观测。
    返回 (seen, done, thread_records)：
    thread_records = 留痕线程的构造现场（kwargs + 构造时兄弟取消是否已收尾）。
    """
    import asyncio
    import threading
    from unittest.mock import patch

    from swarm.brain.nodes.dispatch import dispatch
    from swarm.models.errors import TaskTokenLimitExceeded
    from swarm.types import FileScope, SubTask, TaskPlan

    _mod, seen, done = _spy_audit(monkeypatch, block_event)
    from swarm.project import store as _store
    monkeypatch.setattr(_store, "get_task", lambda *a, **k: None)  # 进度水位读不触 PG

    st_b_settled = threading.Event()

    async def fake_worker(subtask, knowledge_context, project_id="", task_id="", **kw):
        if subtask.id == "st-a":
            # 构造签名=usage dict（models/errors.py:30）——传 str 会在构造期 AttributeError，
            # 被 _run_one 的 except Exception 吞成普通失败对，任务级出口根本不被驱动
            raise TaskTokenLimitExceeded({"task_id": "t-1", "total": 10**9})
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            st_b_settled.set()   # gather 收尾完成的行为见证
            raise

    thread_records: list[dict] = []

    class _ThreadSpy(threading.Thread):
        """子类化真线程（执行器等 incidental 使用者照常工作），只记录留痕线程现场。"""

        def __init__(self, *a, **kw):
            if kw.get("name") == "dispatch-abort-audit":
                thread_records.append({
                    "kw": dict(kw),
                    "settled_before_dispatch": st_b_settled.is_set(),
                })
            super().__init__(*a, **kw)

    monkeypatch.setattr(threading, "Thread", _ThreadSpy)

    plan = TaskPlan(
        subtasks=[
            SubTask(id="st-a", description="a", scope=FileScope(writable=["a.py"])),
            SubTask(id="st-b", description="b", scope=FileScope(writable=["b.py"])),
        ],
        parallel_groups=[["st-a", "st-b"]],
    )
    state = {"task_id": "t-1", "project_id": "p-1", "plan": plan,
             "subtask_results": {}, "dispatch_remaining": ["st-a", "st-b"],
             "failed_subtask_ids": [], "knowledge_context": {}}
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        with pytest.raises(TaskTokenLimitExceeded):
            asyncio.run(dispatch(state))   # 异常必须原样传播（留痕绝不改变异常语义）
    return seen, done, thread_records


def test_dispatch_abort_persists_to_durable_channel(monkeypatch):
    """★LangGraph 在异常时丢弃该 superstep 的【全部】channel 写（26 号文 C-14）★
    subtask_dispatch_totals 的唯一写点在正常出口，于是 cancel / 墙钟 / 预算三条最常见的
    非正常退出下，本批派发在账上从未发生过。治法必须走**不进 state** 的持久通道。

    ★行为级断言（对抗复核用突变实验证伪了初版的 getsource 写法）★
    初版只查源码字面量与顺序：把整段留痕包进 `if os.environ.get("__NEVER__")` 让机制
    彻底成死代码，测试照绿。
    ★批25 GS-5w 换锁★ 原命题=「留痕序 gather < append_task_audit < raise」。
    改为真驱动生产 dispatch 节点到异常出口（不再复刻出口段）：①兄弟任务取消收尾
    （gather）完成后留痕线程才被派发（序）；②持久写经 daemon 线程真实落账且带全量
    payload；③任务级异常原样传播。
    删什么会变红：删留痕块→thread_records 空；留痕挪到 gather 前→settled 标志 False；
    挪到 raise 后→不可达→thread_records 空；改 event/payload→seen 断言红。
    """
    import json

    seen, done, records = _drive_dispatch_abort(monkeypatch)
    assert records, "异常传播前必须已派发留痕线程（留痕块被删/挪到 raise 后此处即红）"
    assert records[0]["settled_before_dispatch"] is True, \
        "留痕不得抢在兄弟任务取消收尾（await gather）之前——顺序是语义的一部分"
    assert done.wait(3), "留痕线程未执行"
    task_id, event, kw = seen[0]
    assert (task_id, event) == ("t-1", "dispatch_aborted")
    assert json.loads(kw["detail"])["spawned"] == ["st-a", "st-b"], \
        "必须带上本批到底派了谁——否则复盘无从还原"


def test_dispatch_abort_trace_is_off_the_event_loop(monkeypatch):
    """★绝不在事件循环里做同步 PG 写（复核 HIGH）★
    append_task_audit → sync_pool().connection()，池 timeout 实测 30s。而本分支的目标
    场景恰是 cancel / 墙钟 / 预算——常伴 PG 压力；brain graph 跑在 API 进程事件循环里，
    一次 30s 阻塞＝整个 API 冻结。也不能 await to_thread：捕的可能就是 CancelledError。

    ★批25 GS-5w 换锁★ 原命题=「abort 留痕异步执行不阻塞事件循环」（源码窗口
    threading.Thread/daemon=True + 否定 to_thread 形态）。行为级：把 append_task_audit
    桩成【不放行就不返回】的阻塞写，真跑生产异常出口——异常必须先传播而留痕仍卡在
    daemon 线程里（fire-and-forget）。若改成内联同步写或 await to_thread（本场景异常
    非 CancelledError，await 会真等），出口卡进阻塞写 → 「传播时留痕未完成」断言变红；
    删 daemon=True / 不起线程 likewise 变红。
    """
    import threading

    gate = threading.Event()   # 故意卡住 PG 写：验证「写不完也照常传播」
    seen, done, records = _drive_dispatch_abort(monkeypatch, block_event=gate)
    # 走到这里 = 异常已传播完毕，而留痕写仍被卡在 daemon 线程里（gate 未放行）
    assert not done.is_set(), \
        "异常传播时留痕写必须尚未完成（内联/await to_thread 会把出口卡进阻塞写）"
    assert records and records[0]["kw"].get("daemon") is True, \
        "留痕必须经 daemon 线程 fire-and-forget（brain graph 跑在 API 进程事件循环上）"
    gate.set()
    assert done.wait(3), "放行后留痕必须真的落账"
    assert seen[0][1] == "dispatch_aborted"


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
