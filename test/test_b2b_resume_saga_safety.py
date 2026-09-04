"""Batch2 第二轮：人工闸恢复 saga 的取消、preflight 与提交 TOCTOU 行为锁。"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import threading
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


async def _post(path: str, payload: dict) -> httpx.Response:
    app_mod = importlib.import_module("swarm.api.app")
    transport = httpx.ASGITransport(app=app_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=payload)


_GATES = [
    ("/api/tasks/t/approve", {}, "DELIVERING"),
    ("/api/tasks/t/revise", {"feedback": "x"}, "DELIVERING"),
    ("/api/tasks/t/reject", {}, "DELIVERING"),
    ("/api/tasks/t/clarify", {"action": "skip"}, "CLARIFYING"),
    ("/api/tasks/t/review-design", {"decision": "approve"}, "DESIGN_REVIEW"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("child_state", ["success", "error"])
async def test_owned_blocking_cancel_reports_late_child_completion(child_state):
    from swarm.infra.cancellation import OwnedBlockingCancelled, run_blocking_owned

    entered = threading.Event()
    release = threading.Event()
    boom = RuntimeError("child failed before side effect")

    def _child():
        entered.set()
        release.wait(timeout=2)
        if child_state == "error":
            raise boom
        return "done"

    call = asyncio.create_task(run_blocking_owned(_child, operation="three-state"))
    assert await asyncio.to_thread(entered.wait, 1)
    call.cancel()
    release.set()
    with pytest.raises(OwnedBlockingCancelled) as caught:
        await call

    assert caught.value.state == child_state
    assert caught.value.result == ("done" if child_state == "success" else None)
    assert caught.value.error is (boom if child_state == "error" else None)


@pytest.mark.asyncio
async def test_owned_blocking_cancel_reports_late_cleanup_failure():
    from swarm.infra.cancellation import OwnedBlockingCancelled, run_blocking_owned

    entered = threading.Event()
    release = threading.Event()
    cleanup_error = RuntimeError("late compensation failed")

    def _child():
        entered.set()
        release.wait(timeout=2)
        return "committed"

    def _cleanup(_result):
        raise cleanup_error

    call = asyncio.create_task(
        run_blocking_owned(
            _child,
            operation="cleanup-three-state",
            cancel_result_cleanup=_cleanup,
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    call.cancel()
    release.set()
    with pytest.raises(OwnedBlockingCancelled) as caught:
        await call

    assert caught.value.state == "cleanup_error"
    assert caught.value.result == "committed"
    assert caught.value.error is cleanup_error


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "payload", "status"), _GATES)
async def test_claim_commit_after_request_cancel_is_deterministically_rolled_back(
    monkeypatch, path, payload, status
):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.runner as runner

    entered = threading.Event()
    release = threading.Event()
    store = MagicMock()
    task = {
        "id": "t",
        "project_id": "p1",
        "status": status,
        "human_decision": "PREVIOUS",
        "merged_diff": "",
    }
    store.get_task.return_value = task

    def _claim(*args, **_kwargs):
        if args[2] != status:
            entered.set()
            release.wait(timeout=2)
            return {**task, "status": "ANALYZING"}
        return task

    store.claim_human_gate.side_effect = _claim
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())

    with patch("swarm.api.routers.task._require_task_access"):
        request_task = asyncio.create_task(_post(path, payload))
        assert await asyncio.to_thread(entered.wait, 1)
        request_task.cancel()
        release.set()
        await asyncio.gather(request_task, return_exceptions=True)
        await asyncio.sleep(0.05)

    rollback_calls = [
        call
        for call in store.claim_human_gate.call_args_list
        if call.args[2] == status
    ]
    claim_calls = [
        call
        for call in store.claim_human_gate.call_args_list
        if call.args[2] != status
    ]
    assert len(claim_calls) == 1
    recovery_saga = claim_calls[0].kwargs["resume_saga"]
    assert recovery_saga["kind"] == "human_gate_claim"
    assert recovery_saga["phase"] == "claimed"
    assert recovery_saga["revert_human_decision"] == "PREVIOUS"
    assert str(uuid.UUID(recovery_saga["saga_id"])) == recovery_saga["saga_id"]
    assert len(rollback_calls) == 1
    assert rollback_calls[0].kwargs["human_decision"] == "PREVIOUS"
    assert rollback_calls[0].kwargs["resume_saga"] == {}
    assert "t" not in runner._task_handles


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "payload", "status"), _GATES)
async def test_real_module_lock_rejection_is_reported_before_200_or_notification(
    monkeypatch, path, payload, status
):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    store = MagicMock()
    task = {"id": "t", "project_id": "p1", "status": status, "merged_diff": ""}
    store.get_task.return_value = task
    store.claim_human_gate.return_value = {**task, "status": "ANALYZING"}
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    monkeypatch.setattr(scheduler, "release_execution_slot", MagicMock())

    lock = MagicMock()
    lock.acquire.return_value = False
    with patch("swarm.api.routers.task._require_task_access"), \
         patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.api.notify.notify", new_callable=AsyncMock) as notify:
        response = await _post(path, payload)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "module_lock_unavailable"
    store.create_notification.assert_not_called()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_api_waiter_cancel_shields_future_and_drains_parked_owner(monkeypatch):
    import swarm.api.routers.task as task_router
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    entered = asyncio.Event()
    rollback = MagicMock()

    async def _slot(task_id):
        scheduler._inflight.add(task_id)
        return scheduler.ExecutionAdmission.SLOTTED

    async def _resume_then_pause(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "await_execution_slot", _slot)
    monkeypatch.setattr(scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    monkeypatch.setattr(runner, "resume_task", _resume_then_pause)
    monkeypatch.setattr(runner.store, "update_task", rollback)
    handle = runner.resume_task_background(
        "t-waiter-cancel", "accept", revert_status="DELIVERING"
    )
    await task_router._await_resume_admission(handle)
    await entered.wait()
    waiter = asyncio.create_task(task_router._await_resume_preflight(handle))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)

    assert not handle.admission.cancelled()
    assert handle.task is not None and handle.task.done()
    assert "t-waiter-cancel" not in scheduler._inflight
    assert "t-waiter-cancel" not in runner._task_handles
    rollback.assert_called_once_with(
        "t-waiter-cancel", status="DELIVERING", resume_saga={}
    )


@pytest.mark.asyncio
async def test_repeated_waiter_cancel_cannot_interrupt_abort_cleanup():
    import swarm.api.routers.task as task_router

    class Handle:
        def __init__(self):
            self.admission = asyncio.get_running_loop().create_future()
            self.abort_entered = asyncio.Event()
            self.abort_release = asyncio.Event()
            self.abort_finished = False

        async def abort(self):
            self.abort_entered.set()
            await self.abort_release.wait()
            self.abort_finished = True

    handle = Handle()
    waiter = asyncio.create_task(task_router._await_resume_admission(handle))
    await asyncio.sleep(0)
    waiter.cancel()
    await handle.abort_entered.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()
    handle.abort_release.set()
    await asyncio.gather(waiter, return_exceptions=True)

    assert handle.abort_finished is True
    assert not handle.admission.cancelled()
    assert waiter.cancelling() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("planning", [False, True])
async def test_cancel_during_module_lock_acquire_cleans_running_lock_slot_and_claim(
    monkeypatch, planning
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    entered = threading.Event()
    release = threading.Event()
    task_id = f"t-lock-cancel-{planning}"
    store = MagicMock()
    store.get_task.return_value = {"id": task_id, "project_id": "p1"}
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    slot_release = MagicMock()
    monkeypatch.setattr(scheduler, "release_execution_slot", slot_release)
    lock = MagicMock()

    def _acquire():
        entered.set()
        release.wait(timeout=2)
        return True

    lock.acquire.side_effect = _acquire
    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock):
        if planning:
            handle = runner.resume_planning_background(
                task_id, {"action": "skip"}, revert_status="CLARIFYING", deferred_start=True
            )
        else:
            handle = runner.resume_task_background(
                task_id, "accept", revert_status="DELIVERING", deferred_start=True
            )
        assert await handle.admission is scheduler.ExecutionAdmission.SLOTTED
        assert handle.start()
        assert await asyncio.to_thread(entered.wait, 1)
        aborting = asyncio.create_task(handle.abort())
        release.set()
        await aborting

    assert task_id not in runner._task_running
    assert task_id not in runner._task_handles
    lock.release.assert_called_once()
    slot_release.assert_called_once_with(task_id)
    store.update_task.assert_called_with(
        task_id,
        status="CLARIFYING" if planning else "DELIVERING",
        resume_saga={},
    )


@pytest.mark.asyncio
async def test_cancel_during_first_post_slot_leadership_check_settles_both_futures(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    entered = asyncio.Event()
    rollback = MagicMock()

    async def _verify():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner.store, "update_task", rollback)
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(scheduler, "verify_local_execution_leadership", _verify)
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    released = MagicMock()
    monkeypatch.setattr(scheduler, "release_execution_slot", released)

    handle = runner.resume_task_background(
        "t-first-verify-cancel", "accept", revert_status="DELIVERING", deferred_start=True
    )
    await entered.wait()
    assert handle.task is not None
    handle.task.cancel()
    await asyncio.gather(handle.task, return_exceptions=True)

    assert handle.admission.done() and not handle.admission.cancelled()
    assert handle.started.done() and not handle.started.cancelled()
    assert (await handle.started).code is runner.ResumeStartCode.CANCELLED
    rollback.assert_called_once_with(
        "t-first-verify-cancel", status="DELIVERING", resume_saga={}
    )
    released.assert_called_once_with("t-first-verify-cancel")


@pytest.mark.asyncio
async def test_second_cancel_during_claim_rollback_still_settles_started(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    rollback_entered = asyncio.Event()

    async def _rollback_claim():
        rollback_entered.set()
        await asyncio.Event().wait()

    async def _run_until_cancelled():
        await asyncio.Event().wait()

    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    monkeypatch.setattr(scheduler, "release_execution_slot", MagicMock())

    handle = runner.ExecutionAdmissionHandle(
        "t-double-cancel-rollback", "DELIVERING", deferred_start=False
    )
    handle.mark_entered()
    monkeypatch.setattr(handle, "rollback_claim", _rollback_claim)
    task = asyncio.create_task(
        runner._run_with_execution_admission(
            handle.task_id,
            "double cancel",
            _run_until_cancelled,
            revert_status="DELIVERING",
            handle=handle,
        )
    )
    handle.bind(task)
    assert await handle.admission is scheduler.ExecutionAdmission.SLOTTED

    task.cancel()
    await rollback_entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert handle.started.done() and not handle.started.cancelled()
    assert (await handle.started).code is runner.ResumeStartCode.ERROR
    assert task.cancelling() == 2


@pytest.mark.asyncio
async def test_guardian_rollback_failure_is_typed_and_never_exposed_as_execution_handle(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    monkeypatch.setattr(runner.store, "update_task", MagicMock(side_effect=RuntimeError("db down")))
    handle = runner.resume_planning_background(
        "t-guardian-error", {"action": "skip"}, revert_status="CLARIFYING"
    )
    assert handle.task is not None
    handle.task.cancel()
    await asyncio.sleep(0)
    await handle.abort()

    assert "t-guardian-error" not in runner._task_handles
    assert await handle.admission is scheduler.ExecutionAdmission.REJECTED_ERROR
    outcome = await handle.started
    assert outcome.code is runner.ResumeStartCode.ERROR
    assert "db down" in outcome.detail


@pytest.mark.asyncio
async def test_leadership_loss_between_checks_rejects_slot(monkeypatch):
    import swarm.brain.scheduler as scheduler
    import swarm.infra.scheduler_leadership as leadership

    class Backend:
        def __init__(self):
            self.calls = 0

        async def verify_leadership(self, _key):
            self.calls += 1
            return self.calls == 1

    backend = Backend()
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    monkeypatch.setattr(scheduler, "_max_concurrent", lambda: 2)
    monkeypatch.setattr(leadership, "get_coordination_backend", lambda: backend)
    scheduler._stopping = False
    scheduler._inflight.discard("t-lease")

    result = await scheduler.await_execution_slot("t-lease")
    assert result is scheduler.ExecutionAdmission.REJECTED_LEADERSHIP_LOST
    assert "t-lease" not in scheduler._inflight


@pytest.mark.asyncio
async def test_cancel_before_background_first_poll_has_guardian_cleanup(monkeypatch):
    import swarm.brain.runner as runner

    rollback = MagicMock()
    monkeypatch.setattr(runner.store, "update_task", rollback)
    handle = runner.resume_planning_background(
        "t-prepoll", {"action": "skip"}, revert_status="CLARIFYING"
    )
    assert handle.task is not None
    handle.task.cancel()
    await handle.abort()

    assert handle.admission.done() and not handle.admission.cancelled()
    assert handle.started.done() and not handle.started.cancelled()
    assert "t-prepoll" not in runner._task_handles
    rollback.assert_called_once_with(
        "t-prepoll", status="CLARIFYING", resume_saga={}
    )


@pytest.mark.asyncio
async def test_resume_return_without_start_outcome_is_rejected_and_rolled_back(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    rollback = MagicMock()
    monkeypatch.setattr(runner.store, "update_task", rollback)
    monkeypatch.setattr(runner, "resume_task", AsyncMock(return_value=None))
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    monkeypatch.setattr(scheduler, "release_execution_slot", MagicMock())

    handle = runner.resume_task_background(
        "t-no-start-outcome",
        "accept",
        revert_status="DELIVERING",
    )
    assert await handle.admission is scheduler.ExecutionAdmission.SLOTTED
    assert handle.task is not None
    await handle.task

    assert handle.started.done()
    assert (await handle.started).code is runner.ResumeStartCode.ERROR
    rollback.assert_called_once_with(
        "t-no-start-outcome", status="DELIVERING", resume_saga={}
    )


@pytest.mark.asyncio
async def test_retry_controller_rejects_live_execution_handle(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    old = asyncio.create_task(asyncio.Event().wait())
    runner._task_handles["t-retry-old"] = old
    observed = []

    async def _retry(*_args, **_kwargs):
        observed.append(runner._task_handles.get("t-retry-old"))
        return False

    monkeypatch.setattr(runner, "retry_task", _retry)
    controller = runner.retry_task_background("t-retry-old")
    result = await controller
    assert result is scheduler.TaskSubmissionResult.REJECTED_ERROR
    assert observed == []
    assert runner._task_handles.get("t-retry-old") is old
    old.cancel()
    await asyncio.gather(old, return_exceptions=True)
    runner._task_handles.pop("t-retry-old", None)


@pytest.mark.asyncio
async def test_stale_start_wrapper_never_removes_replacement_handle(monkeypatch):
    """旧执行晚到 finally 不得把同 task 的新 owner 从注册表删掉。"""
    import swarm.brain.runner as runner

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _run(*_args, **_kwargs):
        entered.set()
        await release.wait()

    monkeypatch.setattr(runner, "run_task", _run)
    runner.start_task_background("t-start-old", "p1", "old")
    old = runner._task_handles["t-start-old"]
    await entered.wait()

    replacement = asyncio.create_task(asyncio.Event().wait())
    runner._task_handles["t-start-old"] = replacement
    release.set()
    await old

    assert runner._task_handles.get("t-start-old") is replacement
    replacement.cancel()
    await asyncio.gather(replacement, return_exceptions=True)
    runner._task_handles.pop("t-start-old", None)


@pytest.mark.asyncio
async def test_stop_during_real_apply_never_rolls_patched_tree_back_to_review(
    monkeypatch, tmp_path
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler
    import swarm.project.diff_apply as diff_apply

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    source = tmp_path / "a.txt"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    patch_text = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"

    task_id = "t-real-apply-stop"
    store = MagicMock()
    store.get_task.return_value = {
        "id": task_id,
        "project_id": "p1",
        "status": "ANALYZING",
        "merged_diff": patch_text,
    }
    store.get_project.return_value = {"id": "p1", "path": str(tmp_path)}
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    released_slot = MagicMock()
    monkeypatch.setattr(scheduler, "release_execution_slot", released_slot)

    lock = MagicMock()
    lock.acquire.return_value = True
    applied = threading.Event()
    release_apply = threading.Event()
    real_apply = diff_apply.apply_git_diff

    def _apply_then_pause(*args, **kwargs):
        result = real_apply(*args, **kwargs)
        applied.set()
        release_apply.wait(timeout=2)
        return result

    monkeypatch.setattr(diff_apply, "apply_git_diff", _apply_then_pause)
    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        handle = runner.resume_task_background(
            task_id,
            "accept",
            revert_status="DELIVERING",
            deferred_start=True,
            apply_diff=True,
        )
        assert await handle.admission is scheduler.ExecutionAdmission.SLOTTED
        assert handle.start() is True
        assert await asyncio.to_thread(applied.wait, 2)
        runner.mark_shutdown_abort(task_id, reason="leadership_lost")
        aborting = asyncio.create_task(handle.abort())
        release_apply.set()
        await aborting

    assert source.read_text(encoding="utf-8") == "new\n"
    assert (await handle.started).code is runner.ResumeStartCode.APPLY_UNCERTAIN
    saga_updates = [
        call.kwargs["resume_saga"]
        for call in store.update_task.call_args_list
        if "resume_saga" in call.kwargs
    ]
    assert saga_updates[-1]["phase"] == "apply_uncertain"
    assert len(saga_updates[-1]["patch_sha256"]) == 64
    assert not any(
        call.kwargs.get("status") == "DELIVERING"
        for call in store.update_task.call_args_list
    )
    released_slot.assert_called_once_with(task_id)
    lock.release.assert_called_once()
    runner.clear_shutdown_abort(task_id)


@pytest.mark.asyncio
async def test_cancelled_apply_child_error_is_preserved_as_uncertain(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler
    import swarm.project.diff_apply as diff_apply

    entered = threading.Event()
    release = threading.Event()
    store = MagicMock()
    store.get_task.return_value = {
        "id": "t-apply-error", "project_id": "p1", "merged_diff": "patch"
    }
    store.get_project.return_value = {"id": "p1", "path": "/tmp"}
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    monkeypatch.setattr(scheduler, "release_execution_slot", MagicMock())
    lock = MagicMock()
    lock.acquire.return_value = True

    def _error_before_side_effect(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=2)
        raise RuntimeError("no side effect")

    monkeypatch.setattr(diff_apply, "apply_git_diff", _error_before_side_effect)
    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        handle = runner.resume_task_background(
            "t-apply-error",
            "accept",
            revert_status="DELIVERING",
            deferred_start=True,
            apply_diff=True,
        )
        await handle.admission
        assert handle.start()
        assert await asyncio.to_thread(entered.wait, 1)
        aborting = asyncio.create_task(handle.abort())
        release.set()
        await aborting

    assert (await handle.started).code is runner.ResumeStartCode.APPLY_UNCERTAIN
    saga_updates = [
        call.kwargs["resume_saga"]
        for call in store.update_task.call_args_list
        if "resume_saga" in call.kwargs
    ]
    assert any(saga.get("phase") == "apply_error" for saga in saga_updates)
    assert saga_updates[-1]["phase"] == "apply_error"
    assert not any(
        call.kwargs.get("status") in {"DELIVERING", "FAILED", "CANCELLED"}
        for call in store.update_task.call_args_list
    )


async def _run_direct_apply_resume(monkeypatch, *, apply_impl, update_side_effect=None):
    import swarm.brain.runner as runner
    import swarm.project.diff_apply as diff_apply

    store = MagicMock()
    store.get_task.return_value = {
        "id": "t-apply-boundary",
        "project_id": "p1",
        "status": "ANALYZING",
        "merged_diff": "patch",
    }
    store.get_project.return_value = {"id": "p1", "path": "/tmp"}
    if update_side_effect is not None:
        store.update_task.side_effect = update_side_effect
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    monkeypatch.setattr(diff_apply, "apply_git_diff", apply_impl)
    lock = MagicMock()
    lock.acquire.return_value = True
    handle = runner.ExecutionAdmissionHandle(
        "t-apply-boundary", "DELIVERING", deferred_start=False
    )
    handle.claim_status = "ANALYZING"
    handle.saga_id = "claim-epoch"
    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        await runner.resume_task(
            "t-apply-boundary",
            "accept",
            revert_status="DELIVERING",
            admission_handle=handle,
            apply_diff=True,
        )
    return runner, store, handle


@pytest.mark.asyncio
async def test_apply_exception_after_applying_saga_stays_recoverable(monkeypatch):
    def _raise_after_unknown_worktree_state(*_args, **_kwargs):
        raise RuntimeError("git apply transport failed")

    runner, store, handle = await _run_direct_apply_resume(
        monkeypatch, apply_impl=_raise_after_unknown_worktree_state
    )

    assert (await handle.started).code is runner.ResumeStartCode.APPLY_UNCERTAIN
    saga_updates = [
        call.kwargs["resume_saga"]
        for call in store.update_task.call_args_list
        if "resume_saga" in call.kwargs
    ]
    assert saga_updates[-1]["phase"] == "apply_error"
    assert not any(
        call.kwargs.get("status") == "FAILED"
        for call in store.update_task.call_args_list
    )


@pytest.mark.asyncio
async def test_applied_marker_write_failure_stays_recoverable(monkeypatch):
    def _update(_task_id, **fields):
        saga = fields.get("resume_saga") or {}
        if saga.get("phase") in {"applied", "apply_error"}:
            raise RuntimeError("database write failed")
        return {"id": "t-apply-boundary", **fields}

    runner, store, handle = await _run_direct_apply_resume(
        monkeypatch,
        apply_impl=lambda *_args, **_kwargs: {"ok": True, "stdout": ""},
        update_side_effect=_update,
    )

    assert (await handle.started).code is runner.ResumeStartCode.APPLY_UNCERTAIN
    phases = [
        (call.kwargs.get("resume_saga") or {}).get("phase")
        for call in store.update_task.call_args_list
        if "resume_saga" in call.kwargs
    ]
    assert store.claim_human_gate.call_args.kwargs["resume_saga"]["phase"] == "applying"
    assert phases[:2] == ["applied", "apply_error"]
    assert "apply_error" in phases
    assert not any(
        call.kwargs.get("status") == "FAILED"
        for call in store.update_task.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("planning", [False, True])
async def test_resume_preflight_exception_rolls_back_claim_without_terminal_saga(
    monkeypatch, planning
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    task_id = f"t-preflight-error-{planning}"
    calls: list[dict] = []

    def _update(_task_id, **fields):
        calls.append(fields)
        if fields.get("status") == "ANALYZING":
            raise RuntimeError("preflight database failure")
        return {"id": task_id, **fields}

    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _tid: {"id": task_id, "project_id": "p1", "merged_diff": ""},
    )
    monkeypatch.setattr(runner.store, "update_task", _update)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    monkeypatch.setattr(scheduler, "release_execution_slot", MagicMock())
    lock = MagicMock()
    lock.acquire.return_value = True

    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        if planning:
            handle = runner.resume_planning_background(
                task_id, {"action": "skip"}, revert_status="CLARIFYING"
            )
        else:
            handle = runner.resume_task_background(
                task_id, "accept", revert_status="DELIVERING"
            )
        assert handle.task is not None
        await asyncio.gather(handle.task, return_exceptions=True)

    assert (await handle.started).code is runner.ResumeStartCode.ERROR
    expected_revert = "CLARIFYING" if planning else "DELIVERING"
    assert calls[-1] == {"status": expected_revert, "resume_saga": {}}
    assert not any(fields.get("status") == "FAILED" for fields in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("planning", [False, True])
async def test_cancel_during_late_successful_claim_transition_rolls_back_exact_start_epoch(
    monkeypatch, planning
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    task_id = f"t-clear-race-{planning}"
    entered = threading.Event()
    release = threading.Event()
    claims: list[tuple[tuple, dict]] = []

    def _claim(*args, **kwargs):
        claims.append((args, kwargs))
        if kwargs.get("expected_saga_id"):
            entered.set()
            release.wait(timeout=2)
        return {"id": task_id, "status": args[2], "resume_saga": kwargs.get("resume_saga", {})}

    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _tid: {"id": task_id, "project_id": "p1", "merged_diff": ""},
    )
    monkeypatch.setattr(runner.store, "claim_human_gate", _claim)
    monkeypatch.setattr(runner.store, "update_task", MagicMock(return_value={"id": task_id}))
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    monkeypatch.setattr(
        scheduler,
        "await_execution_slot",
        AsyncMock(return_value=scheduler.ExecutionAdmission.SLOTTED),
    )
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(scheduler, "register_owned_execution", MagicMock())
    monkeypatch.setattr(scheduler, "release_execution_slot", MagicMock())
    lock = MagicMock()
    lock.acquire.return_value = True
    revert_status = "CLARIFYING" if planning else "DELIVERING"
    claim_status = "ANALYZING"

    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        if planning:
            handle = runner.resume_planning_background(
                task_id, {"action": "skip"}, revert_status=revert_status, deferred_start=True
            )
        else:
            handle = runner.resume_task_background(
                task_id, "accept", revert_status=revert_status, deferred_start=True
            )
        handle.claim_status = claim_status
        handle.saga_id = "claim-epoch"
        await handle.admission
        assert handle.start()
        assert await asyncio.to_thread(entered.wait, 1)
        aborting = asyncio.create_task(handle.abort())
        release.set()
        await aborting

    assert (await handle.started).code is runner.ResumeStartCode.CANCELLED
    assert any(kwargs.get("expected_saga_id") == "claim-epoch" for _, kwargs in claims)
    rollback = claims[-1]
    assert rollback[0][2] == revert_status
    expected = rollback[1].get("expected_resume_saga")
    assert expected == {
        "version": 1,
        "kind": "human_gate_claim",
        "phase": "started",
        "saga_id": "claim-epoch",
        "claimed_status": "ANALYZING",
        "revert_status": revert_status,
        "revert_human_decision": "",
    }


@pytest.mark.asyncio
async def test_cleared_old_start_epoch_has_no_late_rollback_authority(monkeypatch):
    """旧 owner 清账后即使出现 retry ABA，也不得再发空 saga rollback。"""
    import swarm.brain.runner as runner

    calls: list[dict] = []

    def claim(*_args, **kwargs):
        calls.append(kwargs)
        return {"id": "t-aba", "status": "ANALYZING", "resume_saga": kwargs["resume_saga"]}

    monkeypatch.setattr(runner.store, "claim_human_gate", claim)
    handle = runner.ExecutionAdmissionHandle("t-aba", "DELIVERING", deferred_start=True)
    handle.claim_status = "ANALYZING"
    handle.saga_id = "old-epoch"

    assert await handle.transition_claim_for_start("ANALYZING", resume_saga={})
    await handle.clear_claim_for_start()
    # 此刻可发生 cancel→retry→新执行 ANALYZING+saga={}；旧 handle 的迟到 rollback
    # 只能成为 no-op，不能再发 expected_resume_saga={} 的 SQL。
    before = len(calls)
    assert await handle.rollback_claim() is True
    assert len(calls) == before


@pytest.mark.asyncio
async def test_cancel_during_start_epoch_clear_preserves_explicit_started_owner(monkeypatch):
    """clear 迟到成功时 started 已结算，外层会走 owner 的取消终态而非空账回滚。"""
    import swarm.brain.runner as runner

    entered = threading.Event()
    release = threading.Event()

    def claim(*_args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return {"id": "t-clear", "status": "ANALYZING", "resume_saga": kwargs["resume_saga"]}

    monkeypatch.setattr(runner.store, "claim_human_gate", claim)
    handle = runner.ExecutionAdmissionHandle("t-clear", "DELIVERING", deferred_start=True)
    handle.claim_status = "ANALYZING"
    handle.saga_id = "old"
    handle.rollback_expected_saga = {
        "version": 1, "kind": "human_gate_claim", "phase": "started",
        "saga_id": "old", "claimed_status": "ANALYZING",
        "revert_status": "DELIVERING", "revert_human_decision": "",
    }
    handle.complete_started(runner.ResumeStartOutcome(runner.ResumeStartCode.STARTED))

    clearing = asyncio.create_task(handle.clear_claim_for_start())
    assert await asyncio.to_thread(entered.wait, 1)
    clearing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await clearing

    assert (await handle.started).code is runner.ResumeStartCode.STARTED
    assert handle.claim_cleared is True
    assert handle._rollback_done is True


@pytest.mark.asyncio
async def test_reconcile_can_rollback_crashed_durable_start_epoch(monkeypatch):
    """进程崩在 started epoch 清账前时，接管对账可精准恢复审核态。"""
    import swarm.brain.runner as runner

    saga = {
        "version": 1, "kind": "human_gate_claim", "phase": "started",
        "saga_id": "old", "claimed_status": "ANALYZING",
        "revert_status": "DELIVERING", "revert_human_decision": "",
    }
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_settle_resume_saga", settle)
    monkeypatch.setattr(runner, "_audit_reconcile", MagicMock())
    lock = MagicMock()
    lock.acquire.return_value = True
    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock):
        assert await runner._recover_apply_resume_saga({
            "id": "t-started", "project_id": "p", "status": "ANALYZING",
            "resume_saga": saga,
        }) == "recovered"
    assert settle.await_args.kwargs["status"] == "DELIVERING"
    assert settle.await_args.kwargs["resume_saga"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["resume", "apply", "planning"])
async def test_stale_human_claim_cannot_restart_after_reconcile_wins(monkeypatch, mode):
    import swarm.brain.runner as runner
    import swarm.project.diff_apply as diff_apply

    task_id = f"t-stale-claim-{mode}"
    saga = {"version": 1, "kind": "human_gate_claim", "phase": "claimed", "saga_id": "old"}
    task = {
        "id": task_id,
        "project_id": "p1",
        "status": "ANALYZING",
        "merged_diff": "patch" if mode == "apply" else "",
        "resume_saga": saga,
    }
    store = MagicMock()
    store.get_task.return_value = task
    store.get_project.return_value = {"id": "p1", "path": "/tmp"}
    store.claim_human_gate.return_value = None  # reconcile 已按同一 epoch 回滚并清账
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    apply = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(diff_apply, "apply_git_diff", apply)
    lock = MagicMock()
    lock.acquire.return_value = True
    revert = "CLARIFYING" if mode == "planning" else "DELIVERING"
    handle = runner.ExecutionAdmissionHandle(task_id, revert, deferred_start=False)
    handle.claim_status = "ANALYZING"
    handle.saga_id = "old"

    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        if mode == "planning":
            await runner.resume_planning(
                task_id, {"action": "skip"}, revert_status=revert, admission_handle=handle
            )
        else:
            await runner.resume_task(
                task_id,
                "accept",
                revert_status=revert,
                admission_handle=handle,
                apply_diff=mode == "apply",
            )

    assert (await handle.started).code is runner.ResumeStartCode.REJECTED
    store.update_task.assert_not_called()
    apply.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("apply_ok", [True, False])
async def test_apply_phase_cas_never_overwrites_concurrent_cancel_request(
    monkeypatch, apply_ok
):
    import swarm.brain.runner as runner
    import swarm.project.diff_apply as diff_apply

    task_id = f"t-apply-cancel-cas-{apply_ok}"
    old_claim = {"kind": "human_gate_claim", "saga_id": "epoch", "phase": "claimed"}
    task = {
        "id": task_id,
        "project_id": "p1",
        "status": "ANALYZING",
        "merged_diff": "patch",
        "resume_saga": old_claim,
    }
    store = MagicMock()
    store.get_task.return_value = task
    store.get_project.return_value = {"id": "p1", "path": "/tmp"}
    store.claim_human_gate.return_value = {**task, "resume_saga": {"kind": "apply_diff_resume"}}
    # apply 运行期间另一副本已把 cancel_requested=True 写入；旧完整快照 CAS 必 miss。
    store.update_task.return_value = None
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    monkeypatch.setattr(diff_apply, "apply_git_diff", lambda *_a, **_k: {"ok": apply_ok})
    lock = MagicMock()
    lock.acquire.return_value = True
    handle = runner.ExecutionAdmissionHandle(task_id, "DELIVERING", deferred_start=False)
    handle.claim_status = "ANALYZING"
    handle.saga_id = "epoch"

    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        await runner.resume_task(
            task_id,
            "accept",
            revert_status="DELIVERING",
            admission_handle=handle,
            apply_diff=True,
        )

    assert (await handle.started).code is runner.ResumeStartCode.APPLY_UNCERTAIN
    assert store.update_task.call_args_list
    assert all(
        call.kwargs.get("expected_resume_saga")
        for call in store.update_task.call_args_list
        if "resume_saga" in call.kwargs
    )
    assert not any(call.kwargs.get("resume_saga") == {} for call in store.update_task.call_args_list)


@pytest.mark.asyncio
async def test_apply_failed_rollback_uses_full_saga_cas(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.project.diff_apply as diff_apply

    task_id = "t-apply-failed-cancel-race"
    task = {
        "id": task_id,
        "project_id": "p1",
        "status": "ANALYZING",
        "merged_diff": "patch",
        "resume_saga": {"kind": "human_gate_claim", "saga_id": "epoch", "phase": "claimed"},
    }
    store = MagicMock()
    store.get_task.return_value = task
    store.get_project.return_value = {"path": "/tmp"}
    store.claim_human_gate.side_effect = [
        {**task, "resume_saga": {"kind": "apply_diff_resume"}},
        None,  # apply_failed 与 rollback 间 cancel_requested 改写了完整 saga
    ]
    store.update_task.return_value = {"id": task_id}
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "_set_workspace", MagicMock())
    monkeypatch.setattr(runner, "_stop_watchdog", AsyncMock())
    monkeypatch.setattr(diff_apply, "apply_git_diff", lambda *_a, **_k: {"ok": False})
    lock = MagicMock()
    lock.acquire.return_value = True
    handle = runner.ExecutionAdmissionHandle(task_id, "DELIVERING", deferred_start=False)
    handle.claim_status = "ANALYZING"
    handle.saga_id = "epoch"

    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        await runner.resume_task(
            task_id, "accept", revert_status="DELIVERING",
            admission_handle=handle, apply_diff=True,
        )

    assert (await handle.started).code is runner.ResumeStartCode.APPLY_UNCERTAIN
    rollback_kwargs = store.claim_human_gate.call_args_list[-1].kwargs
    assert rollback_kwargs["expected_resume_saga"]["phase"] == "apply_failed"
    assert "expected_saga_id" not in rollback_kwargs


@pytest.mark.asyncio
async def test_parked_resume_handle_is_not_reported_as_orphan(monkeypatch):
    import swarm.brain.runner as runner

    task_id = "t-parked-controller"
    parked = asyncio.create_task(asyncio.Event().wait())
    runner._task_handles[task_id] = parked
    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _tid: {"id": task_id, "status": "ANALYZING"},
    )
    try:
        assert runner.is_task_running(task_id) is True
        assert runner.is_task_orphaned(task_id) is False
    finally:
        parked.cancel()
        await asyncio.gather(parked, return_exceptions=True)
        runner._task_handles.pop(task_id, None)


@pytest.mark.parametrize("state", ["not_applied", "applied", "conflict"])
def test_git_patch_state_uses_forward_and_reverse_checks(tmp_path, state):
    from swarm.project.diff_apply import apply_git_diff, inspect_git_diff_application

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    source = tmp_path / "a.txt"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    patch_text = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
    if state == "applied":
        assert apply_git_diff(str(tmp_path), patch_text)["ok"]
    elif state == "conflict":
        source.write_text("other\n", encoding="utf-8")

    assert inspect_git_diff_application(str(tmp_path), patch_text)["state"] == state


def test_git_patch_state_rejects_bidirectionally_applicable_repeated_context(tmp_path):
    """重复目标行下，ignore-whitespace 可使 forward/reverse 同时成功，必须拒绝猜测。"""
    from swarm.project.diff_apply import apply_git_diff, inspect_git_diff_application

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    source = tmp_path / "a.txt"
    source.write_text(
        "first\nvalue = 1\ntail\nsecond\nvalue = 1\ntail\n",
        encoding="utf-8",
    )
    patch_text = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n"
        " first\n"
        "-value = 1\n+value  = 1\n"
        " tail\n"
    )

    assert apply_git_diff(str(tmp_path), patch_text)["ok"]
    assert source.read_text(encoding="utf-8") == (
        "first\nvalue  = 1\ntail\nsecond\nvalue = 1\ntail\n"
    )
    result = inspect_git_diff_application(str(tmp_path), patch_text)

    assert result["state"] == "conflict"
    assert result["forward"]["ok"] is True
    assert result["reverse"]["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("saga_phase", "patch_state", "expected_status"),
    [
        ("apply_uncertain", "not_applied", "DELIVERING"),
        ("apply_uncertain", "applied", "FAILED"),
        ("apply_uncertain", "conflict", "FAILED"),
        ("apply_failed", "not_applied", "DELIVERING"),
        ("apply_error", "not_applied", "DELIVERING"),
        ("cancel_requested", "not_applied", "CANCELLED"),
        ("cancel_requested", "applied", "CANCELLED"),
    ],
)
async def test_reconcile_consumes_resume_saga_before_active_grace(
    monkeypatch, tmp_path, saga_phase, patch_state, expected_status
):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler
    from swarm.project.diff_apply import apply_git_diff

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    source = tmp_path / "a.txt"
    source.write_text("old\n", encoding="utf-8")
    patch_text = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
    if patch_state == "applied":
        assert apply_git_diff(str(tmp_path), patch_text)["ok"]
    elif patch_state == "conflict":
        source.write_text("other\n", encoding="utf-8")
    import hashlib
    cancel_requested = saga_phase == "cancel_requested"
    stored_phase = "apply_uncertain" if cancel_requested else saga_phase
    rec = {
        "id": f"t-recover-{saga_phase}-{patch_state}",
        "project_id": "p1",
        "description": "x",
        "status": "ANALYZING",
        "merged_diff": patch_text,
        "resume_saga": {
            "version": 1,
            "kind": "apply_diff_resume",
            "saga_id": f"saga-{saga_phase}-{patch_state}",
            "phase": stored_phase,
            "cancel_requested": cancel_requested,
            "patch_sha256": hashlib.sha256(patch_text.encode()).hexdigest(),
            "revert_status": "DELIVERING",
            "revert_human_decision": "REVISE",
        },
    }
    updates = []
    monkeypatch.setattr(runner.store, "list_orphan_candidates", lambda: [rec])
    monkeypatch.setattr(runner.store, "get_project", lambda _pid: {"path": str(tmp_path)})
    monkeypatch.setattr(
        runner.store,
        "claim_human_gate",
        lambda tid, _states, new_status, **kw: updates.append(
            (tid, {**kw, "status": new_status})
        ) or {**rec, **kw, "status": new_status},
    )
    monkeypatch.setattr(scheduler, "is_task_claimed", lambda _tid: False)
    monkeypatch.setattr(runner, "_audit_reconcile", MagicMock())
    lock = MagicMock()
    lock.acquire.return_value = True
    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock):
        stats = await runner.reconcile_orphan_tasks(periodic=True)

    assert stats["resume_saga_recovered"] == 1
    assert updates[-1][1]["status"] == expected_status
    if expected_status == "DELIVERING":
        assert updates[-1][1]["human_decision"] == "REVISE"
    assert updates[-1][1]["resume_saga"]["phase"].startswith("recovered") or (
        updates[-1][1]["resume_saga"]["phase"] == "recovery_conflict"
    )
    lock.release.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_restores_atomic_human_gate_claim_ledger(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    rec = {
        "id": "t-human-claim-recovery",
        "project_id": "p1",
        "description": "x",
        "status": "ANALYZING",
        "resume_saga": {
            "version": 1,
            "kind": "human_gate_claim",
            "saga_id": "saga-human-claim-recovery",
            "phase": "claimed",
            "claimed_status": "ANALYZING",
            "revert_status": "DELIVERING",
            "revert_human_decision": "PREVIOUS",
        },
    }
    updates = []
    monkeypatch.setattr(runner.store, "list_orphan_candidates", lambda: [rec])
    monkeypatch.setattr(
        runner.store,
        "claim_human_gate",
        lambda tid, _states, new_status, **kw: updates.append(
            (tid, {**kw, "status": new_status})
        ) or {**rec, **kw, "status": new_status},
    )
    monkeypatch.setattr(scheduler, "is_task_claimed", lambda _tid: False)
    monkeypatch.setattr(runner, "_audit_reconcile", MagicMock())
    lock = MagicMock()
    lock.acquire.return_value = True
    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock):
        stats = await runner.reconcile_orphan_tasks(periodic=True)

    assert stats["resume_saga_recovered"] == 1
    assert updates[-1][1] == {
        "status": "DELIVERING",
        "human_decision": "PREVIOUS",
        "resume_saga": {},
        "expected_resume_saga": rec["resume_saga"],
    }
    lock.release.assert_called_once()


@pytest.mark.asyncio
async def test_human_gate_claim_recovery_error_is_deferred_not_generic_failed(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    rec = {
        "id": "t-human-claim-defer",
        "project_id": "p1",
        "description": "x",
        "status": "ANALYZING",
        "resume_saga": {
            "version": 1,
            "kind": "human_gate_claim",
            "phase": "claimed",
            "claimed_status": "ANALYZING",
            "revert_status": "DELIVERING",
        },
    }
    updates = MagicMock()
    monkeypatch.setattr(runner.store, "list_orphan_candidates", lambda: [rec])
    monkeypatch.setattr(runner.store, "update_task", updates)
    monkeypatch.setattr(scheduler, "is_task_claimed", lambda _tid: False)
    monkeypatch.setattr(
        runner,
        "_recover_apply_resume_saga",
        AsyncMock(side_effect=RuntimeError("temporary db failure")),
    )

    stats = await runner.reconcile_orphan_tasks(periodic=False)

    assert stats["resume_saga_deferred"] == 1
    updates.assert_not_called()


@pytest.mark.asyncio
async def test_stale_resume_saga_recovery_cannot_overwrite_new_execution_epoch(monkeypatch):
    import swarm.brain.runner as runner

    rec = {
        "id": "t-stale-saga",
        "project_id": "p1",
        "status": "ANALYZING",
        "resume_saga": {
            "version": 1,
            "kind": "human_gate_claim",
            "saga_id": "old-epoch",
            "phase": "claimed",
            "claimed_status": "ANALYZING",
            "revert_status": "DELIVERING",
        },
    }
    settle = MagicMock(return_value=None)
    monkeypatch.setattr(runner.store, "claim_human_gate", settle)
    audit = MagicMock()
    monkeypatch.setattr(runner, "_audit_reconcile", audit)
    lock = MagicMock()
    lock.acquire.return_value = True

    with patch("swarm.infra.redis_client.ModuleLock", return_value=lock):
        result = await runner._recover_apply_resume_saga(rec)

    assert result == "deferred"
    settle.assert_called_once()
    assert settle.call_args.kwargs["expected_resume_saga"]["saga_id"] == "old-epoch"
    audit.assert_not_called()
    lock.release.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_skips_live_resume_handle_waiting_for_slot(monkeypatch):
    import swarm.brain.runner as runner

    task_id = "t-parked-resume"
    rec = {
        "id": task_id,
        "project_id": "p1",
        "status": "ANALYZING",
        "resume_saga": {
            "version": 1,
            "kind": "human_gate_claim",
            "saga_id": "parked-epoch",
            "phase": "claimed",
            "claimed_status": "ANALYZING",
            "revert_status": "DELIVERING",
        },
    }
    parked = asyncio.create_task(asyncio.Event().wait())
    runner._task_handles[task_id] = parked
    monkeypatch.setattr(runner.store, "list_orphan_candidates", lambda: [rec])
    settle = MagicMock()
    monkeypatch.setattr(runner.store, "claim_human_gate", settle)
    try:
        stats = await runner.reconcile_orphan_tasks(periodic=True)
    finally:
        parked.cancel()
        await asyncio.gather(parked, return_exceptions=True)
        runner._task_handles.pop(task_id, None)

    assert stats["skipped_running"] == 1
    settle.assert_not_called()


@pytest.mark.parametrize("kind", ["human_gate_claim", "apply_diff_resume", "execute_claim"])
def test_retry_rejects_active_task_with_pending_saga_epoch(monkeypatch, kind):
    import swarm.brain.runner as runner

    task_id = f"t-pending-{kind}"
    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _tid: {
            "id": task_id,
            "status": "ANALYZING",
            "resume_saga": {"kind": kind, "saga_id": "pending-epoch"},
        },
    )
    runner._task_running.discard(task_id)
    runner._task_handles.pop(task_id, None)

    allowed, reason = runner.can_retry_task(task_id)

    assert allowed is False
    assert "待恢复事务" in reason


@pytest.mark.parametrize("kind", ["human_gate_claim", "apply_diff_resume", "execute_claim"])
def test_retry_rejects_terminal_task_with_unresolved_saga_epoch(monkeypatch, kind):
    import swarm.brain.runner as runner

    task_id = f"t-terminal-pending-{kind}"
    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _tid: {
            "id": task_id,
            "status": "FAILED",
            "resume_saga": {
                "kind": kind,
                "phase": "applying" if kind == "apply_diff_resume" else "claimed",
                "saga_id": "pending-epoch",
            },
        },
    )
    runner._task_running.discard(task_id)
    runner._task_handles.pop(task_id, None)

    allowed, reason = runner.can_retry_task(task_id)

    assert allowed is False
    assert "待恢复事务" in reason


@pytest.mark.asyncio
async def test_cancel_marks_apply_saga_for_recovery_without_terminalizing(monkeypatch):
    import swarm.brain.runner as runner

    saga = {
        "version": 1,
        "kind": "apply_diff_resume",
        "phase": "applying",
        "saga_id": "apply-epoch",
        "patch_sha256": "abc",
    }
    task = {
        "id": "t-cancel-apply",
        "project_id": "p1",
        "status": "ANALYZING",
        "resume_saga": saga,
    }
    claim = MagicMock(return_value={**task, "resume_saga": {**saga, "cancel_requested": True}})
    update = MagicMock()
    monkeypatch.setattr(runner.store, "get_task", lambda _tid: task)
    monkeypatch.setattr(runner.store, "claim_human_gate", claim)
    monkeypatch.setattr(runner.store, "update_task", update)
    recover = AsyncMock(return_value="recovered")
    monkeypatch.setattr(runner, "_recover_apply_resume_saga", recover)
    monkeypatch.setattr(
        runner, "_cancel_proof_machine_account", AsyncMock(return_value={"cancel_origin": "api_cancel"})
    )
    runner._task_handles.pop(task["id"], None)
    runner._task_queues.pop(task["id"], None)
    with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        assert await runner.cancel_task(task["id"]) is True

    claim.assert_called_once()
    kwargs = claim.call_args.kwargs
    assert claim.call_args.args[1:] == ({"ANALYZING"}, "ANALYZING")
    assert kwargs["expected_resume_saga"] == saga
    assert kwargs["resume_saga"]["cancel_requested"] is True
    assert kwargs["token_usage"]["cancel_origin"] == "api_cancel"
    recover.assert_awaited_once()
    update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["human_gate_claim", "execute_claim", "retry_claim"])
async def test_cancel_atomically_clears_safe_execution_claim(monkeypatch, kind):
    import swarm.brain.runner as runner

    task = {
        "id": f"t-cancel-{kind}",
        "project_id": "p1",
        "status": "SUBMITTED" if kind != "human_gate_claim" else "ANALYZING",
        "resume_saga": {"kind": kind, "phase": "claimed", "saga_id": "epoch"},
    }
    claim = MagicMock(return_value={**task, "status": "CANCELLED", "resume_saga": {}})
    monkeypatch.setattr(runner.store, "get_task", lambda _tid: task)
    monkeypatch.setattr(runner.store, "claim_human_gate", claim)
    monkeypatch.setattr(runner.store, "update_task", MagicMock())
    monkeypatch.setattr(
        runner, "_cancel_proof_machine_account", AsyncMock(return_value={"cancel_origin": "api_cancel"})
    )
    runner._task_handles.pop(task["id"], None)
    runner._task_queues.pop(task["id"], None)
    with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        assert await runner.cancel_task(task["id"]) is True

    assert claim.call_args.args[1:] == ({task["status"]}, "CANCELLED")
    assert claim.call_args.kwargs["resume_saga"] == {}
    assert claim.call_args.kwargs["expected_resume_saga"] == task["resume_saga"]


@pytest.mark.asyncio
async def test_cancel_request_disconnect_waits_for_owned_runner_cleanup(monkeypatch):
    import swarm.brain.runner as runner

    task_id = "t-cancel-owner-drain"
    cleaning = asyncio.Event()
    release = asyncio.Event()
    claim = MagicMock(return_value={"id": task_id, "status": "CANCELLED"})
    manager = MagicMock()

    async def _owned_runner():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()

    child = asyncio.create_task(_owned_runner())
    await asyncio.sleep(0)
    runner._task_handles[task_id] = child
    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _tid: {"id": task_id, "status": "ANALYZING", "resume_saga": {}},
    )
    monkeypatch.setattr(runner.store, "claim_human_gate", claim)
    monkeypatch.setattr(
        runner, "_cancel_proof_machine_account", AsyncMock(return_value={"cancel_origin": "api_cancel"})
    )

    with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=manager):
        cancelling = asyncio.create_task(runner.cancel_task(task_id))
        await cleaning.wait()
        cancelling.cancel()
        await asyncio.sleep(0)
        assert not cancelling.done()
        manager.kill_by_task.assert_not_called()
        claim.assert_not_called()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelling

    manager.kill_by_task.assert_called_once_with(task_id)
    assert claim.call_args.args[2] == "CANCELLED"
    runner._task_handles.pop(task_id, None)


@pytest.mark.asyncio
async def test_plain_cancel_cas_does_not_overwrite_concurrent_retry_epoch(monkeypatch):
    import swarm.brain.runner as runner

    task_id = "t-plain-cancel-retry-race"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _account(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"cancel_origin": "api_cancel"}

    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _tid: {"id": task_id, "status": "ANALYZING", "resume_saga": {}},
    )
    claim = MagicMock(return_value=None)  # account 等待期间 retry 已写入新 saga，CAS miss
    monkeypatch.setattr(runner.store, "claim_human_gate", claim)
    monkeypatch.setattr(runner, "_cancel_proof_machine_account", _account)
    runner._task_handles.pop(task_id, None)
    runner._task_queues.pop(task_id, None)

    with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=MagicMock()):
        cancelling = asyncio.create_task(runner.cancel_task(task_id))
        await entered.wait()
        release.set()
        assert await cancelling is False

    assert claim.call_args.kwargs["expected_resume_saga"] == {}
    assert claim.call_args.args[1:] == ({"ANALYZING"}, "CANCELLED")


@pytest.mark.asyncio
async def test_remote_active_cancel_lock_busy_has_zero_settlement_or_kill(monkeypatch):
    """无本地 handle 时拿不到项目宽锁，必须 fail-closed，不能结算或误杀远端资源。"""
    import swarm.brain.runner as runner

    task_id = "t-remote-lock-busy"
    task = {
        "id": task_id,
        "project_id": "p-remote",
        "status": "ANALYZING",
        "resume_saga": {},
    }
    claim = MagicMock()
    manager = MagicMock()
    account = AsyncMock(return_value={"cancel_origin": "api_cancel"})

    class BusyLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            return False

        def release(self):
            raise AssertionError("未取得的锁不得释放")

    monkeypatch.setattr(runner.store, "get_task", lambda _tid: task)
    monkeypatch.setattr(runner.store, "claim_human_gate", claim)
    monkeypatch.setattr(runner, "_cancel_proof_machine_account", account)
    runner._task_handles.pop(task_id, None)
    runner._task_queues.pop(task_id, None)

    with patch("swarm.infra.redis_client.ModuleLock", BusyLock), \
         patch("swarm.worker.sandbox.get_sandbox_manager", return_value=manager):
        assert await runner.cancel_task(task_id) is False

    claim.assert_not_called()
    account.assert_not_awaited()
    manager.kill_by_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["human_gate_claim", "execute_claim", "retry_claim"])
async def test_reconcile_clears_historical_terminal_safe_claim(monkeypatch, kind):
    import swarm.brain.runner as runner

    rec = {
        "id": f"t-terminal-{kind}",
        "project_id": "p1",
        "status": "CANCELLED",
        "resume_saga": {"version": 1, "kind": kind, "phase": "claimed", "saga_id": "epoch"},
    }
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "_settle_resume_saga", settle)

    assert await runner._recover_apply_resume_saga(rec) == "recovered"
    assert settle.await_args.kwargs["status"] == "CANCELLED"
    assert settle.await_args.kwargs["resume_saga"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saga",
    [
        {"version": 2, "kind": "apply_diff_resume", "phase": "applying"},
        {"version": 1, "kind": "apply_diff_resume", "phase": "future_phase"},
        {"version": 1, "kind": "human_gate_claim", "phase": "future_phase"},
        {"version": 1, "kind": "execute_claim", "phase": "future_phase"},
        {"version": 1, "kind": "retry_claim", "phase": "future_phase"},
    ],
)
async def test_known_unknown_saga_never_falls_into_generic_orphan_recovery(saga):
    import swarm.brain.runner as runner

    result = await runner._recover_apply_resume_saga(
        {"id": "t-unknown-saga", "status": "ANALYZING", "resume_saga": saga}
    )

    assert result == "deferred"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["recovered_applied", "recovered_not_applied", "recovery_conflict"]
)
async def test_settled_apply_saga_no_longer_blocks_normal_state_reconcile(phase):
    import swarm.brain.runner as runner

    result = await runner._recover_apply_resume_saga({
        "id": "t-settled",
        "status": "DELIVERING",
        "resume_saga": {"version": 1, "kind": "apply_diff_resume", "phase": phase},
    })

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "execute"])
async def test_submission_rechecks_scheduler_at_actual_enqueue_point(monkeypatch, kind):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler

    store = MagicMock()
    store.get_project.return_value = {
        "id": "p1", "status": "READY", "graph_status": "INDEXED"
    }
    store.get_progress.return_value = {
        "phase": "complete",
        "index_stats": {"symbols": 1},
        "embed_stats": {"vectors": 1},
    }
    store.find_active_duplicate_task.return_value = None
    store.create_task.side_effect = lambda **_kw: (
        setattr(scheduler, "_stopping", True)
        or {"id": "t", "project_id": "p1", "status": "SUBMITTED"}
    )
    pooled = {
        "id": "t", "project_id": "p1", "description": "x", "status": "POOLED"
    }
    store.get_task.return_value = pooled

    def _update(_task_id, **fields):
        return {**pooled, **fields}

    store.update_task.side_effect = _update

    def _claim(_task_id, _states, new_status, **_kwargs):
        if new_status == "SUBMITTED":
            scheduler._stopping = True
            return {**pooled, "status": "SUBMITTED"}
        return {**pooled, "status": new_status}

    store.claim_human_gate.side_effect = _claim
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    scheduler._stopping = False
    try:
        with patch("swarm.api.routers.task._require_task_access"):
            if kind == "create":
                response = await _post(
                    "/api/projects/p1/tasks", {"description": "x", "force": True}
                )
            else:
                response = await _post("/api/tasks/t/execute", {})
    finally:
        scheduler._stopping = False

    assert response.status_code == 503, response.text
    if kind == "execute":
        assert any(
            call.args[2] == "POOLED"
            for call in store.claim_human_gate.call_args_list
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["create", "execute", "retry"])
async def test_route_guard_true_but_submit_leadership_false_is_machine_rejected(
    monkeypatch, kind
):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    store = MagicMock()
    project = {"id": "p1", "status": "READY", "graph_status": "INDEXED"}
    pooled = {
        "id": "t",
        "project_id": "p1",
        "description": "x",
        "status": "POOLED",
        "auto_accept": False,
        "queue_priority": "normal",
    }
    retryable = {**pooled, "status": "FAILED", "thread_id": "old"}
    store.get_project.return_value = project
    store.get_progress.return_value = {
        "phase": "complete",
        "index_stats": {"symbols": 1},
        "embed_stats": {"vectors": 1},
    }
    store.find_active_duplicate_task.return_value = None
    store.create_task.return_value = {**pooled, "status": "SUBMITTED"}
    store.get_task.return_value = retryable if kind == "retry" else pooled
    store.claim_human_gate.return_value = {**pooled, "status": "SUBMITTED"}
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    submit = AsyncMock(return_value=scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST)
    monkeypatch.setattr(scheduler, "submit_task", submit)

    with patch("swarm.api.routers.task._require_task_access"), \
         patch.object(runner, "can_retry_task", return_value=(True, "")):
        if kind == "create":
            response = await _post(
                "/api/projects/p1/tasks", {"description": "x", "force": True}
            )
        elif kind == "execute":
            response = await _post("/api/tasks/t/execute", {})
        else:
            response = await _post("/api/tasks/t/retry", {})

    assert response.status_code == 503, response.text
    assert "leadership_lost" in response.text
    if kind == "execute":
        claim, rollback = store.claim_human_gate.call_args_list
        assert claim.args[2] == "SUBMITTED"
        assert claim.kwargs["auto_accept"] is False
        assert claim.kwargs["queue_priority"] == "normal"
        execute_saga = claim.kwargs["resume_saga"]
        assert execute_saga["kind"] == "execute_claim"
        assert rollback.args[2] == "POOLED"
        assert rollback.kwargs["auto_accept"] is False
        assert rollback.kwargs["queue_priority"] == "normal"
        assert rollback.kwargs["resume_saga"] == {}
        assert rollback.kwargs["expected_saga_id"] == execute_saga["saga_id"]
        store.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_real_submit_rechecks_leadership_before_enqueue(monkeypatch):
    import swarm.brain.scheduler as scheduler

    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)
    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", AsyncMock(return_value=False)
    )
    enqueue = MagicMock()
    monkeypatch.setattr(scheduler.TaskQueue, "enqueue", enqueue)
    scheduler._stopping = False

    result = await scheduler.submit_task("t-submit-lease", "p1", "x")

    assert result is scheduler.TaskSubmissionResult.REJECTED_LEADERSHIP_LOST
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_submit_rechecks_stopping_after_yielding_leadership_probe(monkeypatch):
    import swarm.brain.scheduler as scheduler

    monkeypatch.setattr(scheduler, "is_consumer_running", lambda: True)

    async def _leadership_then_stop():
        await asyncio.sleep(0)
        scheduler._stopping = True
        return True

    monkeypatch.setattr(
        scheduler, "verify_local_execution_leadership", _leadership_then_stop
    )
    enqueue = MagicMock()
    monkeypatch.setattr(scheduler.TaskQueue, "enqueue", enqueue)
    scheduler._stopping = False
    try:
        result = await scheduler.submit_task("t-submit-stop-race", "p1", "x")
    finally:
        scheduler._stopping = False

    assert result is scheduler.TaskSubmissionResult.REJECTED_STOPPING
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_execute_claims_once_and_submits_once(monkeypatch):
    app_mod = importlib.import_module("swarm.api.app")
    import swarm.brain.scheduler as scheduler

    store = MagicMock()
    pooled = {
        "id": "t", "project_id": "p1", "description": "x", "status": "POOLED"
    }
    store.get_task.return_value = pooled
    claims = 0

    def _claim(*_args, **_kwargs):
        nonlocal claims
        claims += 1
        return {**pooled, "status": "SUBMITTED"} if claims == 1 else None

    store.claim_human_gate.side_effect = _claim
    monkeypatch.setattr(app_mod, "store", store)
    monkeypatch.setattr(app_mod, "require_execution_plane_ready", AsyncMock())
    submit = AsyncMock(return_value=scheduler.TaskSubmissionResult.ENQUEUED)
    monkeypatch.setattr(scheduler, "submit_task", submit)

    with patch("swarm.api.routers.task._require_task_access"):
        first, second = await asyncio.gather(
            _post("/api/tasks/t/execute", {}),
            _post("/api/tasks/t/execute", {}),
        )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dequeue_rechecks_leadership_before_dispatch(monkeypatch):
    import swarm.brain.runner as runner
    import swarm.brain.scheduler as scheduler

    items = [{"task_id": "t-dispatch-lease", "priority": "normal"}]
    monkeypatch.setattr(scheduler.TaskQueue, "supports_blocking", lambda: False)
    monkeypatch.setattr(
        scheduler.TaskQueue, "dequeue", lambda **_kwargs: items.pop(0) if items else None
    )
    requeued = MagicMock()
    monkeypatch.setattr(scheduler.TaskQueue, "enqueue", requeued)
    monkeypatch.setattr(
        scheduler,
        "_resolve_exec_meta",
        lambda _tid: {"project_id": "p1", "description": "x", "auto_accept": False},
    )
    monkeypatch.setattr(scheduler, "_project_exec_admission", lambda _pid: "ready")
    monkeypatch.setattr(scheduler, "_maybe_drain_stranded", AsyncMock())
    monkeypatch.setattr(scheduler, "verify_local_execution_leadership", AsyncMock(return_value=False))
    started = MagicMock()
    monkeypatch.setattr(runner, "start_task_background", started)
    scheduler._consumer_started = False
    scheduler._consumer_task = None
    scheduler._inflight.discard("t-dispatch-lease")

    try:
        await scheduler.start_task_scheduler()
        await asyncio.sleep(0.02)
        await scheduler.stop_task_scheduler()

        started.assert_not_called()
        assert "t-dispatch-lease" not in scheduler._inflight
        requeued.assert_called_with("t-dispatch-lease", "p1", priority="normal")
    finally:
        if scheduler.is_consumer_running():
            await scheduler.stop_task_scheduler()
        scheduler._stopping = False
