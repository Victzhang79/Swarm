#!/usr/bin/env python3
"""P0-4 交付通道记账治本行为测试（D07 merge_conflicts 只写不清 + D26 增量 output 喂全量 sync）。

D07：_sync_task_from_state 对 merge_conflicts 用 `is not None` 下发（含空列表清空 DB），
     retry_task 重置补 abandoned_subtasks=0 / merge_conflicts=[]。
D26：on_chain_end 累积节点增量 output 成全量快照后再喂 _sync_task_from_state——abandoned/
     completed 只被"知全量值"的写入更新，绝不被不含该键的 dispatch 增量覆盖成 0；且 dispatch
     增量无 plan 时仍据累积 plan 正确过滤 completed。

行为断言（禁 getsource）：直接观测写库参数 / 数据流。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import runner


# ── D07：merge_conflicts 只写不清 ─────────────────────────────


def test_sync_clears_merge_conflicts_with_empty_list(monkeypatch):
    """merge 干净轮 state.merge_conflicts=[] → 必须下发 merge_conflicts=[] 清 DB。

    旧 truthiness `if merge_conflicts:` 下空列表永不下发 → DB 残留首轮冲突 → /apply-diff 永久 409。
    """
    calls: list[dict] = []
    monkeypatch.setattr(runner.store, "update_task", lambda tid, **kw: calls.append(kw))

    runner._sync_task_from_state("t1", {"merge_conflicts": []})

    assert len(calls) == 1
    assert "merge_conflicts" in calls[0]
    assert calls[0]["merge_conflicts"] == []


def test_sync_conflict_round_then_clean_round_unblocks_apply_diff(monkeypatch):
    """第 1 轮冲突入库 → 恢复后第 2 轮干净合并 → DB 冲突被清空 → apply-diff 消费端放行。"""
    calls: list[dict] = []
    monkeypatch.setattr(runner.store, "update_task", lambda tid, **kw: calls.append(kw))

    # 第 1 轮：merge 产出冲突
    conflict = [{"file_path": "pom.xml", "subtask_ids": ["st-1", "st-2"], "message": "conflict"}]
    runner._sync_task_from_state("t1", {"merge_conflicts": conflict})
    assert calls[-1]["merge_conflicts"] == conflict

    # 第 2 轮（恢复后干净合并，brain merge 节点已把 state 清成 []）
    runner._sync_task_from_state("t1", {"merge_conflicts": []})
    stored = calls[-1]["merge_conflicts"]
    assert stored == []

    # api/routers/task.py /apply-diff 消费契约：`conflicts = task.get("merge_conflicts") or []`
    # 非空即 409。清空后 → falsy → 放行。
    assert not (stored or [])


def test_sync_omits_merge_conflicts_when_key_absent(monkeypatch):
    """state 未触及 merge_conflicts（None）→ 不下发该字段，保留 DB 现值（不误清）。"""
    calls: list[dict] = []
    monkeypatch.setattr(runner.store, "update_task", lambda tid, **kw: calls.append(kw))

    runner._sync_task_from_state("t1", {"merged_diff": "diff --git a b"})

    # 有写入（merged_diff），但绝不含 merge_conflicts（None 时跳过，不用 [] 误覆盖 DB）。
    assert calls, "应有其它字段写入"
    assert all("merge_conflicts" not in c for c in calls)


async def test_retry_task_resets_abandoned_and_merge_conflicts(monkeypatch):
    """retry_task 必须以新 claim epoch 覆写上轮残留 saga（执行账/冲突的清零在消费端原子完成）。"""
    # 乱序鲁棒（同 test_scheduler.py:191 先例）：前面任何测试文件最后调过
    # stop_task_scheduler() 会把模块全局 `_stopping` 留为 True（复位只在
    # start_task_scheduler 里）→ retry_task 被 REJECTED_STOPPING 早退；同理钉死
    # is_consumer_running=False 保证走 allow_no_scheduler 的直跑路径而非 submit_task。
    from swarm.brain import scheduler as _sched
    monkeypatch.setattr(_sched, "_stopping", False)
    monkeypatch.setattr(_sched, "is_consumer_running", lambda: False)

    calls: list[dict] = []
    monkeypatch.setattr(runner, "can_retry_task", lambda tid: (True, ""))
    monkeypatch.setattr(
        runner.store, "get_task",
        lambda tid: {"project_id": "p", "description": "d"},
    )
    # 两阶段 retry（D41/E2）：update_task 的返回值是 CAS claim 成败信号，None=未命中被拒；
    # stub 必须返回非 None 表示认领成功。
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: calls.append(kw) or {"id": tid})

    async def _noop_run(*a, **k):
        return None

    monkeypatch.setattr(runner, "run_task", _noop_run)
    runner._task_running.discard("tid")

    ok = await runner.retry_task("tid", allow_no_scheduler=True)
    assert ok is True

    reset = next((c for c in calls if c.get("status") == "SUBMITTED"), None)
    assert reset is not None
    # 两阶段 retry（D41/E2，13e270c/f7b8d53）：retry_task 同步段只写【新 claim epoch】——
    # 覆写任何上轮残留 resume_saga、换新 thread_id、带 expected_* CAS 守卫；
    # 执行账/冲突清零（plan={}/merged_diff=""/abandoned_subtasks=0/merge_conflicts=[]/
    # resume_saga={}）挪到执行器消费 retry_claim epoch 时原子完成，覆盖在
    # test_b2b_execution_admission_handshake.py::
    # test_run_task_atomically_consumes_retry_claim_before_reset。
    saga = reset["resume_saga"]
    assert saga.get("kind") == "retry_claim" and saga.get("saga_id"), \
        "claim 必须以全新 retry_claim saga 覆写上轮残留 resume_saga"
    assert reset["thread_id"].startswith("tid-r-"), "claim 必须换新 thread_id（新 epoch）"
    assert "expected_status" in reset and "expected_thread_id" in reset, \
        "claim 必须带 CAS 守卫，拒绝陈旧重跑"


# ── D26：增量 output 喂全量 sync 系统性错账 ─────────────────────


class _FakeTopic:
    """替身 _FanoutTopic：仅记录 publish 的事件。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


class _FakeSnapshot:
    def __init__(self, values: dict) -> None:
        self.values = values
        self.interrupts = None


class _FakeGraph:
    """替身 compiled brain graph：吐脚本化的 astream_events 事件序列。"""

    def __init__(self, events: list[dict], final_values: dict) -> None:
        self._events = events
        self._final = final_values

    async def astream_events(self, graph_input, config=None, version=None):
        for ev in self._events:
            yield ev

    async def aget_state(self, config):
        return _FakeSnapshot(self._final)


def _wire_common(monkeypatch, calls: list[dict]):
    monkeypatch.setattr(
        runner.store, "get_task",
        lambda tid: {
            "thread_id": "th", "project_id": "p",
            "description": "d", "complexity": None, "plan": None,
        },
    )
    monkeypatch.setattr(runner.store, "update_task", lambda tid, **kw: calls.append(kw))
    monkeypatch.setattr(runner.store, "check_task_token_limit", lambda *a, **k: (True, {}))


async def test_dispatch_after_handle_failure_does_not_clobber_abandoned(monkeypatch):
    """handle_failure 放弃 st-3 后紧跟 dispatch 的 output 同步不把 abandoned 清零（D26-a）。

    dispatch 的 output 恒含 subtask_results、从不含 abandoned_subtask_ids；旧实现每次 dispatch
    算 abandoned=0 写库，把 handle_failure 刚放弃的清零。修后据累积全量快照 → abandoned=1。
    """
    calls: list[dict] = []
    _wire_common(monkeypatch, calls)

    events = [
        {"event": "on_chain_end", "name": "plan",
         "data": {"output": {"plan": {"subtasks": [{"id": "st-1"}, {"id": "st-2"}, {"id": "st-3"}]}}}},
        # handle_failure 放弃 st-3（不在 _SYNC_ON_NODES，仅累积）
        {"event": "on_chain_end", "name": "handle_failure",
         "data": {"output": {"abandoned_subtask_ids": ["st-3"]}}},
        # dispatch 增量只带 subtask_results（无 abandoned 键）——旧代码在此清零
        {"event": "on_chain_end", "name": "dispatch",
         "data": {"output": {"subtask_results": {
             "st-1": {"l1_passed": True}, "st-2": {"l1_passed": True}}}}},
    ]
    fake = _FakeGraph(events, {"plan": {"subtasks": [{"id": "st-1"}, {"id": "st-2"}, {"id": "st-3"}]}})
    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: fake)

    await runner._stream_brain_events("tid", {"description": "d"}, _FakeTopic(),
                                      project_id="p", lock_holder=None)

    sync_calls = [c for c in calls if "abandoned_subtasks" in c]
    assert sync_calls, "dispatch 同步应写记账"
    last = sync_calls[-1]
    assert last["abandoned_subtasks"] == 1, "handle_failure 放弃的 st-3 不得被 dispatch 增量清零"
    assert last["completed_subtasks"] == 2


async def test_dispatch_completed_filtered_by_accumulated_plan(monkeypatch):
    """dispatch 增量无 plan 键，仍据累积 plan 过滤 completed，不数已不在当前 plan 的旧 id（D26-b）。

    subtask_results 含 replan 后已废弃的 st-old（不在当前 plan）。旧实现 _plan_subtask_ids 因增量无
    plan 返 None → 数全部结果(=3) 且夹紧失效 → completed 超分母。修后按累积 plan 过滤 → 2。
    """
    calls: list[dict] = []
    _wire_common(monkeypatch, calls)

    events = [
        {"event": "on_chain_end", "name": "plan",
         "data": {"output": {"plan": {"subtasks": [{"id": "st-1"}, {"id": "st-2"}]}}}},
        {"event": "on_chain_end", "name": "dispatch",
         "data": {"output": {"subtask_results": {
             "st-1": {"l1_passed": True},
             "st-2": {"l1_passed": True},
             "st-old": {"l1_passed": True},  # replan 前的旧 id，不在当前 plan
         }}}},
    ]
    fake = _FakeGraph(events, {"plan": {"subtasks": [{"id": "st-1"}, {"id": "st-2"}]}})
    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: fake)

    await runner._stream_brain_events("tid", {"description": "d"}, _FakeTopic(),
                                      project_id="p", lock_holder=None)

    sync_calls = [c for c in calls if "completed_subtasks" in c]
    assert sync_calls
    last = sync_calls[-1]
    assert last["subtask_count"] == 2
    assert last["completed_subtasks"] == 2, "只数当前 plan 内通过项，旧 st-old 不计"
    assert last["completed_subtasks"] <= last["subtask_count"]
