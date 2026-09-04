"""Batch 2-B/E：API 必须等执行准入确认，不能 fire-and-forget 假 200。"""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


async def _post(path: str, payload: dict) -> httpx.Response:
    from swarm.api.app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=payload)


@pytest.fixture(autouse=True)
def _local_leadership(monkeypatch):
    import swarm.brain.scheduler as scheduler

    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True)
    )


@pytest.mark.asyncio
async def test_result_resume_rejection_rolls_back_before_503_without_apply_or_notify(
    monkeypatch,
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler
    app_mod = importlib.import_module("swarm.api.app")

    task = {
        "id": "t-result-reject",
        "project_id": "p1",
        "status": "DELIVERING",
        "merged_diff": "diff --git a/a b/a\n",
    }
    claimed = {**task, "status": "ANALYZING", "human_decision": "ACCEPT"}
    app_store = MagicMock()
    app_store.get_task.return_value = task
    app_store.get_project.return_value = {"id": "p1", "path": "/tmp/p1"}
    app_store.claim_human_gate.return_value = claimed
    rollback = MagicMock()
    apply_diff = MagicMock(return_value={"ok": True})
    notify = AsyncMock()

    monkeypatch.setattr(app_mod.app.state, "lifespan_active", True, raising=False)
    monkeypatch.setattr(app_mod, "store", app_store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    monkeypatch.setattr(runner.store, "update_task", rollback)
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.REJECTED_UNAVAILABLE),
    )
    monkeypatch.setattr(runner, "resume_task", AsyncMock())

    cfg = MagicMock()
    cfg.sandbox.sandbox_first = True
    with patch("swarm.api.routers.task._require_task_access"), \
         patch("swarm.api.app.get_config", return_value=cfg), \
         patch("swarm.project.diff_apply.apply_git_diff", apply_diff), \
         patch("swarm.api.notify.notify", notify):
        response = await _post(
            "/api/tasks/t-result-reject/approve", {"apply_diff": True}
        )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == {
        "code": "execution_admission_rejected",
        "reason": "rejected_unavailable",
    }
    rollback.assert_called_once_with(
        "t-result-reject", status="DELIVERING", resume_saga={}, human_decision=""
    )
    apply_diff.assert_not_called()
    app_store.create_notification.assert_not_called()
    notify.assert_not_awaited()
    runner.resume_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_planning_resume_rejection_rolls_back_before_503_and_never_runs(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler
    app_mod = importlib.import_module("swarm.api.app")

    task = {"id": "t-plan-reject", "project_id": "p1", "status": "CLARIFYING"}
    app_store = MagicMock()
    app_store.get_task.return_value = task
    app_store.claim_human_gate.return_value = {**task, "status": "ANALYZING"}
    rollback = MagicMock()

    monkeypatch.setattr(app_mod.app.state, "lifespan_active", True, raising=False)
    monkeypatch.setattr(app_mod, "store", app_store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    monkeypatch.setattr(runner.store, "update_task", rollback)
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.REJECTED_TIMEOUT),
    )
    resume = AsyncMock()
    monkeypatch.setattr(runner, "resume_planning", resume)

    with patch("swarm.api.routers.task._require_task_access"):
        response = await _post("/api/tasks/t-plan-reject/clarify", {"action": "skip"})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["reason"] == "rejected_timeout"
    rollback.assert_called_once_with(
        "t-plan-reject", status="CLARIFYING", resume_saga={}, human_decision=""
    )
    resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_deferred_resume_starts_once_only_after_admission_and_releases_owned_slot(
    monkeypatch,
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    run_calls = []

    async def run(*_args, admission_handle=None, **_kwargs):
        run_calls.append(True)
        admission_handle.complete_started(
            runner.ResumeStartOutcome(runner.ResumeStartCode.STARTED)
        )
    registered = MagicMock()
    released = MagicMock()
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", registered)
    monkeypatch.setattr(scheduler, "release_execution_slot", released)
    monkeypatch.setattr(runner, "resume_task", run)

    handle = runner.resume_task_background(
        "t-handshake",
        "accept",
        revert_status="DELIVERING",
        deferred_start=True,
    )
    admission = await handle.admission

    assert admission is scheduler.ExecutionAdmission.SLOTTED
    registered.assert_called_once()
    assert run_calls == []
    handle.start()
    handle.start()
    assert (await handle.started).code is runner.ResumeStartCode.STARTED
    await handle.task
    assert run_calls == [True]
    released.assert_called_once_with("t-handshake")


@pytest.mark.asyncio
async def test_admitted_handle_cancelled_before_start_reports_failure_after_rollback(
    monkeypatch,
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    rollback = MagicMock()
    released = MagicMock()
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    monkeypatch.setattr(scheduler, "release_execution_slot", released)
    monkeypatch.setattr(runner.store, "update_task", rollback)
    monkeypatch.setattr(runner, "resume_task", AsyncMock())

    handle = runner.resume_task_background(
        "t-cancelled-before-start",
        "accept",
        revert_status="DELIVERING",
        deferred_start=True,
    )
    assert await handle.admission is scheduler.ExecutionAdmission.SLOTTED
    assert handle.task is not None
    handle.task.cancel()
    await asyncio.gather(handle.task, return_exceptions=True)

    assert handle.start() is False
    assert (await handle.started).code is runner.ResumeStartCode.CANCELLED
    rollback.assert_called_once_with(
        "t-cancelled-before-start", status="DELIVERING", resume_saga={}
    )
    released.assert_called_once_with("t-cancelled-before-start")


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_admission_completes_handshake_after_rollback(
    monkeypatch,
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    entered = asyncio.Event()
    rollback = MagicMock()

    async def _wait_forever(_task_id):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "await_execution_slot", _wait_forever)
    monkeypatch.setattr(runner.store, "update_task", rollback)
    monkeypatch.setattr(runner, "resume_planning", AsyncMock())

    handle = runner.resume_planning_background(
        "t-cancel-admission",
        {"action": "skip"},
        revert_status="CLARIFYING",
        deferred_start=True,
    )
    await entered.wait()
    assert handle.task is not None
    handle.task.cancel()
    await asyncio.gather(handle.task, return_exceptions=True)

    assert await handle.admission is scheduler.ExecutionAdmission.REJECTED_STOPPING
    assert (await handle.started).code is runner.ResumeStartCode.CANCELLED
    rollback.assert_called_once_with(
        "t-cancel-admission", status="CLARIFYING", resume_saga={}
    )


@pytest.mark.asyncio
async def test_approve_does_not_return_200_if_stop_cancels_parked_resume(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler
    app_mod = importlib.import_module("swarm.api.app")

    task = {
        "id": "t-stop-before-start",
        "project_id": "p1",
        "status": "DELIVERING",
        "merged_diff": "",
    }
    app_store = MagicMock()
    app_store.get_task.return_value = task
    app_store.get_project.return_value = {"id": "p1", "path": "/tmp/p1"}
    app_store.claim_human_gate.return_value = {**task, "status": "ANALYZING"}
    rollback = MagicMock()
    resume = AsyncMock()

    monkeypatch.setattr(app_mod.app.state, "lifespan_active", True, raising=False)
    monkeypatch.setattr(app_mod, "store", app_store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    monkeypatch.setattr(runner.store, "update_task", rollback)
    monkeypatch.setattr(runner, "resume_task", resume)
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )

    def _register_then_stop(_task_id):
        current = asyncio.current_task()
        assert current is not None
        asyncio.get_running_loop().call_soon(current.cancel)

    monkeypatch.setattr(scheduler, "register_owned_execution", _register_then_stop)
    monkeypatch.setattr(scheduler, "release_execution_slot", MagicMock())

    with patch("swarm.api.routers.task._require_task_access"), \
         patch("swarm.api.notify.notify", new_callable=AsyncMock) as notify:
        response = await _post("/api/tasks/t-stop-before-start/approve", {})

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["reason"] == "cancelled"
    rollback.assert_called_once_with(
        "t-stop-before-start", status="DELIVERING", resume_saga={}, human_decision=""
    )
    resume.assert_not_awaited()
    app_store.create_notification.assert_not_called()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_consumer_race_returns_503_without_resetting_task(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler
    import swarm.infra.scheduler_leadership as leadership
    app_mod = importlib.import_module("swarm.api.app")

    task = {
        "id": "t-retry-race",
        "project_id": "p1",
        "status": "FAILED",
        "description": "retry",
    }
    app_store = MagicMock()
    app_store.get_task.return_value = task
    monkeypatch.setattr(app_mod.app.state, "lifespan_active", True, raising=False)
    monkeypatch.setattr(app_mod, "store", app_store)
    monkeypatch.setattr(runner, "can_retry_task", lambda _task_id: (True, ""))
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    consumer = MagicMock(side_effect=[True, False])
    monkeypatch.setattr(scheduler, "is_consumer_running", consumer)
    monkeypatch.setattr(leadership, "get_coordination_backend", lambda: None)

    with patch("swarm.api.routers.task._require_task_access"):
        response = await _post("/api/tasks/t-retry-race/retry", {})

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == {
        "code": "execution_admission_rejected",
        "reason": "rejected_unavailable",
    }
    app_store.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_retry_rechecks_consumer_after_cancelling_old_execution(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    task_id = "t-retry-cancel-race"
    updates = MagicMock()
    submit = MagicMock()
    monkeypatch.setattr(runner, "can_retry_task", lambda _task_id: (True, ""))
    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _task_id: {
            "id": task_id,
            "project_id": "p1",
            "description": "retry",
            "status": "FAILED",
        },
    )
    monkeypatch.setattr(runner.store, "update_task", updates)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    consumer_alive = True
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: consumer_alive)
    monkeypatch.setattr(scheduler, "submit_task", submit)

    async def _cancel(_task_id):
        nonlocal consumer_alive
        consumer_alive = False
        runner._task_running.discard(task_id)
        return True

    monkeypatch.setattr(runner, "cancel_task", _cancel)
    runner._task_running.add(task_id)
    try:
        assert await runner.retry_task(task_id) is False
    finally:
        runner._task_running.discard(task_id)

    updates.assert_not_called()
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_retry_reset_rolls_back_when_local_enqueue_loses_leader(monkeypatch):
    """本地 enqueue 明确拒绝时按 retry epoch 无损回滚，不能留会在未来偷偷执行的 SUBMITTED。"""
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    task_id = "t-retry-durable"
    prior = {
        "id": task_id,
        "project_id": "p1",
        "description": "retry",
        "status": "FAILED",
        "thread_id": "old-thread",
        "queue_priority": "normal",
        "auto_accept": True,
        "base_commit": "old-base",
    }
    claimed: list[dict] = []

    def _claim(_task_id, **fields):
        claimed.append(fields)
        return {**prior, **fields}

    monkeypatch.setattr(runner, "can_retry_task", lambda _task_id: (True, ""))
    monkeypatch.setattr(runner.store, "get_task", lambda _task_id: prior)
    monkeypatch.setattr(runner.store, "update_task", _claim)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "submit_task",
        AsyncMock(return_value=scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST),
    )

    result = await runner.retry_task(
        task_id, auto_accept=False, return_submission_result=True
    )

    assert result is scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST
    assert claimed[0]["status"] == "SUBMITTED"
    assert claimed[0]["expected_status"] == "FAILED"
    assert claimed[0]["expected_thread_id"] == "old-thread"
    assert claimed[0]["resume_saga"]["kind"] == "retry_claim"
    assert claimed[0]["resume_saga"]["phase"] == "submitted"
    assert claimed[0]["auto_accept"] is False
    assert "base_commit" not in claimed[0]
    assert claimed[1]["status"] == "FAILED"
    assert claimed[1]["thread_id"] == "old-thread"
    assert claimed[1]["auto_accept"] is True
    assert claimed[1]["resume_saga"] == {}
    assert claimed[1]["expected_saga_id"] == claimed[0]["resume_saga"]["saga_id"]
    assert "base_commit" not in claimed[1]


@pytest.mark.asyncio
async def test_retry_claim_cas_miss_does_not_enqueue(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    task_id = "t-retry-cas-miss"
    monkeypatch.setattr(runner, "can_retry_task", lambda _task_id: (True, ""))
    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _task_id: {
            "id": task_id,
            "project_id": "p1",
            "description": "retry",
            "status": "FAILED",
            "thread_id": "old-thread",
        },
    )
    monkeypatch.setattr(runner.store, "update_task", MagicMock(return_value=None))
    submit = AsyncMock()
    monkeypatch.setattr(scheduler, "submit_task", submit)
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)

    result = await runner.retry_task(task_id, return_submission_result=True)

    assert result is scheduler.TaskSubmissionResult.REJECTED_ERROR
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_rollback_cas_miss_reports_already_consumed(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler

    pooled = {
        "id": "t-execute-consumed",
        "project_id": "p1",
        "description": "pooled",
        "status": "POOLED",
        "auto_accept": False,
        "queue_priority": "normal",
    }
    consumed = {**pooled, "status": "ANALYZING", "resume_saga": {}}
    app_store = MagicMock()
    app_store.get_task.side_effect = [pooled, consumed]
    app_store.claim_human_gate.side_effect = [
        {**pooled, "status": "SUBMITTED"},
        None,  # scheduler/runner 已消费 epoch，陈旧 rollback 不得谎报“未接收”
    ]
    monkeypatch.setattr(app_mod, "store", app_store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    monkeypatch.setattr(
        scheduler,
        "submit_task",
        AsyncMock(return_value=scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST),
    )

    with patch("swarm.api.routers.task._require_task_access"):
        response = await _post("/api/tasks/t-execute-consumed/execute", {})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert response.json()["task"]["status"] == "ANALYZING"


@pytest.mark.asyncio
async def test_execute_rollback_cas_miss_from_cancel_is_not_false_success(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler

    pooled = {
        "id": "t-execute-cancelled",
        "project_id": "p1",
        "description": "pooled",
        "status": "POOLED",
    }
    cancelled = {**pooled, "status": "CANCELLED", "resume_saga": {}}
    app_store = MagicMock()
    app_store.get_task.side_effect = [pooled, cancelled]
    app_store.claim_human_gate.side_effect = [
        {**pooled, "status": "SUBMITTED"}, None,
    ]
    monkeypatch.setattr(app_mod, "store", app_store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    monkeypatch.setattr(
        scheduler,
        "submit_task",
        AsyncMock(return_value=scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST),
    )

    with patch("swarm.api.routers.task._require_task_access"):
        response = await _post("/api/tasks/t-execute-cancelled/execute", {})

    assert response.status_code == 503
    assert response.json()["detail"]["persisted_for_recovery"] is False


@pytest.mark.asyncio
async def test_retry_rollback_cas_miss_from_cancel_is_not_false_enqueued(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    task_id = "t-retry-cancelled-race"
    prior = {
        "id": task_id, "project_id": "p1", "description": "retry",
        "status": "FAILED", "thread_id": "old", "auto_accept": False,
    }
    cancelled = {**prior, "status": "CANCELLED", "thread_id": "new", "resume_saga": {}}
    monkeypatch.setattr(runner, "can_retry_task", lambda _tid: (True, ""))
    monkeypatch.setattr(runner.store, "get_task", MagicMock(side_effect=[prior, cancelled]))
    monkeypatch.setattr(
        runner.store,
        "update_task",
        MagicMock(side_effect=[{**prior, "status": "SUBMITTED", "thread_id": "new"}, None]),
    )
    monkeypatch.setattr(scheduler, "is_stopping", lambda: False)
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "submit_task",
        AsyncMock(return_value=scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST),
    )

    result = await runner.retry_task(task_id, return_submission_result=True)

    assert result is scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST


@pytest.mark.asyncio
async def test_run_task_atomically_consumes_retry_claim_before_reset(monkeypatch):
    import swarm.brain.runner as runner

    task_id = "t-consume-retry"
    saga = {"version": 1, "kind": "retry_claim", "phase": "submitted", "saga_id": "epoch"}
    rec = {
        "id": task_id,
        "project_id": "p1",
        "description": "retry",
        "status": "SUBMITTED",
        "thread_id": "new-thread",
        "base_commit": "old-base",
        "resume_saga": saga,
    }
    store = MagicMock()
    store.get_task.return_value = rec
    store.update_task.side_effect = lambda _tid, **fields: {**rec, **fields}
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_best_effort_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(runner, "_failed_machine_account", MagicMock(return_value={}))
    monkeypatch.setattr(runner, "_emit_task_notification", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    lock = MagicMock()
    lock.acquire.return_value = True

    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.memory.profile.load_profile_prompts", side_effect=RuntimeError("stop after consume")), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        await runner.run_task(task_id, "p1", "retry")

    consume = store.update_task.call_args_list[0].kwargs
    assert consume["expected_status"] == "SUBMITTED"
    assert consume["expected_thread_id"] == "new-thread"
    assert consume["expected_saga_id"] == "epoch"
    assert consume["plan"] == {}
    assert consume["merged_diff"] == ""
    assert consume["base_commit"] == ""
    assert consume["resume_saga"] == {}
