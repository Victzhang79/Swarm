"""阶段5 批2（登记册 §六）：E1 retry 播种 / E4 watchdog / E7 周期对账 / E12 fail-fast /
E13 挂起 TTL 提醒。（E6 pool saver 由 test_checkpointer_require.py 语义演进覆盖；
E8 to_thread/E9 approve 持锁为接线级改动，行为由既有端点/调度测试兜底。）
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from swarm.types import Confidence, WorkerOutput

# ─────────────── E1：retry 播种 ───────────────


class _FakeLock:
    def acquire(self):
        return True

    def release(self):
        return True

    def renew(self):
        return True


def _wo(sid, l1=True):
    return WorkerOutput(subtask_id=sid, diff="+x\n", summary="", l1_passed=l1,
                        confidence=Confidence.HIGH if l1 else Confidence.LOW)


def test_e1_retry_seed_consumed_into_initial_state():
    from swarm.brain import runner

    seen: dict = {}

    async def _capture_stream(task_id, graph_input, queue, **kw):
        seen["initial"] = graph_input
        raise RuntimeError("stop here")  # 播种验证完即止（走泛 except FAILED）

    prev_state = {
        "plan": {"subtasks": [{"id": "st-1"}]},
        "subtask_results": {"st-1": _wo("st-1", l1=True), "st-2": _wo("st-2", l1=False)},
        "coverage_watermark": ["req-1", "req-2"],
    }
    store_mock = MagicMock()
    store_mock.get_task.return_value = {"retry_prev_thread_id": "t-e1-old",
                                        "thread_id": "t-e1-r-abcd"}
    store_mock.get_project.return_value = None

    with patch.object(runner, "_stream_brain_events", side_effect=_capture_stream), \
         patch.object(runner, "_load_state_snapshot",
                      AsyncMock(return_value=prev_state)) as load_mock, \
         patch.object(runner, "store", store_mock), \
         patch.object(runner, "audit", MagicMock()), \
         patch.object(runner, "_set_workspace", MagicMock()), \
         patch.object(runner, "_emit_task_notification", MagicMock()), \
         patch("swarm.infra.redis_client.ModuleLock", lambda *a, **k: _FakeLock()), \
         patch("swarm.memory.profile.load_profile_prompts", return_value=({}, "", "")), \
         patch("swarm.git_base.capture_base_commit", return_value=None), \
         patch("swarm.memory.session.build_session_metadata", return_value={}):
        asyncio.run(runner.run_task("t-e1", "p-e1", "desc"))

    # R65REPLAY-T8：泛 except 兜底现会再取一次快照（best-effort 机读账/清扫——治后
    # 恒生效，旧死代码 _accumulated_state 从不触发此二次读）。retry-seed 是【首次】读，
    # 须带上一执行段 thread；第二次是终态兜底（无 thread_id kwarg）。
    assert load_mock.await_count == 2, \
        f"retry-seed 首读 + 泛 except 兜底二读: 实际 {load_mock.await_count}"
    _seed_call = load_mock.await_args_list[0]
    assert _seed_call.kwargs.get("thread_id") == "t-e1-old", "首次读必须是上一执行段 thread（retry 播种）"
    initial = seen["initial"]
    assert set(initial.get("subtask_results", {})) == {"st-1"}, (
        "retry 播种只带【L1 通过】产物——旧 retry 从零重跑把已付工作整批作废")
    assert initial.get("plan") is prev_state["plan"]
    assert initial.get("coverage_watermark") == ["req-1", "req-2"], "覆盖单调合同跨 retry 延续"
    assert any(kw.get("retry_prev_thread_id") == ""
               for _, kw in store_mock.update_task.call_args_list), "指针一次性消费必清"


def test_e1_seed_failure_degrades_to_fresh_run():
    from swarm.brain import runner
    seen: dict = {}

    async def _capture_stream(task_id, graph_input, queue, **kw):
        seen["initial"] = graph_input
        raise RuntimeError("stop")

    store_mock = MagicMock()
    store_mock.get_task.return_value = {"retry_prev_thread_id": "t-old"}
    store_mock.get_project.return_value = None
    with patch.object(runner, "_stream_brain_events", side_effect=_capture_stream), \
         patch.object(runner, "_load_state_snapshot",
                      AsyncMock(side_effect=RuntimeError("pg down"))), \
         patch.object(runner, "store", store_mock), \
         patch.object(runner, "audit", MagicMock()), \
         patch.object(runner, "_set_workspace", MagicMock()), \
         patch.object(runner, "_emit_task_notification", MagicMock()), \
         patch("swarm.infra.redis_client.ModuleLock", lambda *a, **k: _FakeLock()), \
         patch("swarm.memory.profile.load_profile_prompts", return_value=({}, "", "")), \
         patch("swarm.git_base.capture_base_commit", return_value=None), \
         patch("swarm.memory.session.build_session_metadata", return_value={}):
        asyncio.run(runner.run_task("t-e1b", "p", "d"))
    assert "plan" not in seen["initial"], "播种失败=纯增益降级（从零重跑=旧行为），绝不阻断"


# ─────────────── E4：watchdog 中止登记 ───────────────

async def test_e4_watchdog_abort_goes_salvage():
    from swarm.brain import runner
    runner._watchdog_abort["t-e4"] = runner.TaskWallclockExceeded(100.0, 200.0)
    with patch.object(runner, "_salvage_partial_from_checkpoint", AsyncMock()) as sal:
        handled = await runner._maybe_salvage_watchdog_abort("t-e4", MagicMock())
    assert handled is True
    assert sal.await_args.kwargs.get("reason_code") == "wallclock_exceeded"
    assert "t-e4" not in runner._watchdog_abort, "登记一次性消费"


async def test_e4_true_cancel_untouched():
    from swarm.brain import runner
    with patch.object(runner, "_salvage_partial_from_checkpoint", AsyncMock()) as sal:
        handled = await runner._maybe_salvage_watchdog_abort("t-none", MagicMock())
    assert handled is False and sal.await_count == 0, "真人工取消原语义（CANCELLED）不被劫持"


# ─────────────── E7：周期对账跳过 SUBMITTED 重入队 ───────────────

def _reconcile_with(rec, periodic):
    from swarm.brain import runner
    submit = MagicMock()
    with patch.object(runner.store, "list_orphan_candidates", return_value=[rec]), \
         patch("swarm.brain.scheduler.submit_task", submit), \
         patch("swarm.brain.scheduler.is_task_claimed", return_value=False):
        stats = asyncio.run(runner.reconcile_orphan_tasks(periodic=periodic))
    return submit, stats


def test_e7_periodic_skips_submitted_requeue():
    rec = {"id": "t-sub", "status": "SUBMITTED", "project_id": "p", "description": "d"}
    submit, _ = _reconcile_with(rec, periodic=True)
    submit.assert_not_called(), (
        "TaskQueue.enqueue 无去重——周期重入队=队列膨胀；队列/调度器本就持有 SUBMITTED")


def test_e7_startup_still_requeues_submitted():
    rec = {"id": "t-sub2", "status": "SUBMITTED", "project_id": "p", "description": "d"}
    submit, _ = _reconcile_with(rec, periodic=False)
    submit.assert_called_once(), "启动模式照旧重入队（Redis 可能被清空，唯一恢复通道）"


# ─────────────── E12：ERROR 项目 fail-fast 三态 ───────────────

def test_e12_admission_tristate():
    from swarm.brain import scheduler as sch

    def _with_status(st):
        with patch("swarm.project.store.get_project",
                   return_value=({"status": st} if st else None)):
            return sch._project_exec_admission("p-1")

    assert _with_status("READY") == "ready"
    assert _with_status("BUILDING") == "wait"
    assert _with_status("ERROR") == "error", (
        "ERROR=预处理已明确失败——留池 200 次×3s 后强制放行=注定失败的执行白烧")
    # #76 DR-02-F3 治本：记录【确实缺失】(get_project 读到 None) → fail-fast（无沙箱/索引，放行=
    # 注定失败白烧一整轮 worker + 占槽位）。黄灯坐实 create_task 已硬校验项目存在、无延迟创建窗口。
    # 注：读【异常】仍保守 ready（DB 抖动不卡队列，走 except 分支，另见 test_b2_orchestration_fixes）。
    assert _with_status(None) == "error", "记录确实缺失→fail-fast（#76：不放行注定失败的执行）"


# ─────────────── E13：挂起态 TTL 提醒 ───────────────

def test_e13_ttl_notifies_once_per_period(monkeypatch):
    import datetime as dt

    from swarm.brain import runner
    monkeypatch.setenv("SWARM_INTERRUPT_TTL_NOTIFY_H", "1")
    runner._ttl_notified.clear()
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    rec = {"id": "t-ttl", "status": "CONFIRMING", "updated_at": old, "description": "d"}
    notify = MagicMock()
    with patch.object(runner.store, "list_orphan_candidates", return_value=[rec]), \
         patch.object(runner, "_emit_task_notification", notify):
        n1 = asyncio.run(runner.check_suspended_ttl())
        n2 = asyncio.run(runner.check_suspended_ttl())
    assert n1 == 1, "超 TTL 的挂起任务必须升级提醒（漏配 auto_accept=无限等待的可观测出口）"
    assert n2 == 0, "TTL 周期内不重复轰炸"
    runner._ttl_notified.clear()


def test_e13_fresh_suspended_not_notified(monkeypatch):
    import datetime as dt

    from swarm.brain import runner
    monkeypatch.setenv("SWARM_INTERRUPT_TTL_NOTIFY_H", "24")
    runner._ttl_notified.clear()
    rec = {"id": "t-fresh", "status": "DELIVERING",
           "updated_at": dt.datetime.now(dt.timezone.utc), "description": "d"}
    with patch.object(runner.store, "list_orphan_candidates", return_value=[rec]), \
         patch.object(runner, "_emit_task_notification", MagicMock()):
        n = asyncio.run(runner.check_suspended_ttl())
    assert n == 0, "TTL 内的合法人工等待绝不打扰（不强杀是拍板口径）"


# ─────────────── 5.9 猎手 F1（CRITICAL）：进度派生对无 plan 状态安全 ───────────────

def test_f1_progress_derivation_survives_no_plan():
    """_plan_subtask_ids 无 plan 返回 None——len(None) 曾让每个 fresh/retry 任务在
    首个节点事件 TypeError 整单 FAILED（测试盲区：批1 只测了 dispatch 回写面）。"""
    from swarm.brain.runner import _plan_subtask_ids
    _p_ids = _plan_subtask_ids({})          # fresh submit / retry 清空 plan 的形态
    _p_total = len(_p_ids) if _p_ids else 0  # 修后表达式（与 runner 同款）
    assert _p_total == 0
    assert _plan_subtask_ids({"plan": {}}) is None
    assert _plan_subtask_ids({"plan": {"subtasks": []}}) is None
