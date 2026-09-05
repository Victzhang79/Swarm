"""批次 4A 双透镜发现的生命周期根因回归。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm.types import Confidence, FileScope, SubTask, WorkerOutput
from swarm.worker.executor import WorkerExecutor
from swarm.worker import executor as executor_module


@pytest.fixture(autouse=True)
def _quarantine_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_INSTANCE_STATE_DIR", str(tmp_path / "instance-state"))


def _executor(tmp_path: Path, scope: FileScope) -> WorkerExecutor:
    subtask = SubTask(id="st-b4-root", description="x", scope=scope)
    executor = WorkerExecutor(subtask, project_path=str(tmp_path))
    executor._build_manifest_files = lambda: []
    executor._module_source_files = lambda: []
    executor._resolve_project_stack = lambda: {}
    return executor


def test_failed_worker_discovery_restores_shared_tree_before_retry(tmp_path):
    sibling = tmp_path / "src" / "Sibling.py"
    sibling.parent.mkdir()
    sibling.write_text("before\n")
    executor = _executor(
        tmp_path,
        FileScope(writable=["src/Main.py"], readable=["src/Sibling.py"]),
    )
    executor._remember_worker_discovered_baseline("src/Sibling.py")
    executor._worker_discovered_paths.add("src/Sibling.py")
    sibling.write_text("poisoned\n")
    executor._record_worker_discovered_outputs(
        {"src/Sibling.py": executor._worker_path_snapshot(sibling)}
    )

    errors = executor._rollback_worker_discovered_paths()

    assert errors == []
    assert sibling.read_text() == "before\n"
    assert not executor._worker_discovered_paths


def test_discovered_rollback_uses_cas_and_preserves_concurrent_sibling(tmp_path):
    sibling = tmp_path / "sibling.py"
    sibling.write_text("baseline\n")
    executor = _executor(tmp_path, FileScope(writable=["main.py"]))
    executor._remember_worker_discovered_baseline("sibling.py")
    executor._worker_discovered_paths.add("sibling.py")
    sibling.write_text("worker poison\n")
    worker_snapshot = executor._worker_path_snapshot(sibling)
    # 并发兄弟恰在 pull-back 写盘与 provenance 入账之间提交正确内容。
    sibling.write_text("sibling good\n")
    executor._record_worker_discovered_outputs(
        {"sibling.py": worker_snapshot}
    )

    errors = executor._rollback_worker_discovered_paths()

    assert errors and "CONCURRENT_CHANGE" in errors[0]
    assert sibling.read_text() == "sibling good\n"


def test_pullback_cas_refuses_to_overwrite_concurrent_sibling(
    tmp_path, monkeypatch
):
    from swarm.worker import sandbox as sandbox_module

    sibling = tmp_path / "sibling.py"
    sibling.write_text("baseline\n")
    baseline = WorkerExecutor._worker_path_snapshot(sibling)
    sibling.write_text("sibling good\n")
    manager = sandbox_module.SandboxManager.__new__(sandbox_module.SandboxManager)
    manager.config = SimpleNamespace(sandbox_remote_workdir="/workspace")
    manager._preserve_line_endings = lambda _path, data: data
    manager._record_sandbox_success = lambda _sid: None
    manager._record_sandbox_failure = lambda _sid: None
    monkeypatch.setattr(
        sandbox_module,
        "read_file_from_sandbox",
        lambda *_a, **_k: b"worker poison\n",
    )

    stats = manager.sync_files_from_sandbox(
        SimpleNamespace(sandbox_id="sb"),
        tmp_path,
        ["sibling.py"],
        expected_snapshots={"sibling.py": baseline},
    )

    assert stats["conflicts"] == ["sibling.py"]
    assert stats["downloaded"] == 0
    assert sibling.read_text() == "sibling good\n"


def test_pullback_batch_conflict_is_all_or_none(tmp_path, monkeypatch):
    from swarm.worker import sandbox as sandbox_module

    (tmp_path / "a.py").write_text("a0\n")
    (tmp_path / "b.py").write_text("b0\n")
    expected = {
        name: WorkerExecutor._worker_path_snapshot(tmp_path / name)
        for name in ("a.py", "b.py")
    }
    (tmp_path / "b.py").write_text("concurrent\n")
    manager = sandbox_module.SandboxManager.__new__(sandbox_module.SandboxManager)
    manager.config = SimpleNamespace(sandbox_remote_workdir="/workspace")
    manager._preserve_line_endings = lambda _path, data: data
    monkeypatch.setattr(
        sandbox_module,
        "read_file_from_sandbox",
        lambda _sandbox, remote, **_kwargs: f"remote:{remote}\n".encode(),
    )

    stats = manager.sync_files_from_sandbox(
        SimpleNamespace(sandbox_id="sb"),
        tmp_path,
        ["a.py", "b.py"],
        expected_snapshots=expected,
    )

    assert stats["conflicts"] == ["b.py"]
    assert stats["written_snapshots"] == {}
    assert (tmp_path / "a.py").read_text() == "a0\n"
    assert (tmp_path / "b.py").read_text() == "concurrent\n"


def test_successful_pullback_preserves_existing_executable_mode(tmp_path, monkeypatch):
    from swarm.worker import sandbox as sandbox_module

    target = tmp_path / "run.sh"
    target.write_text("old\n")
    target.chmod(0o755)
    expected = WorkerExecutor._worker_path_snapshot(target)
    manager = sandbox_module.SandboxManager.__new__(sandbox_module.SandboxManager)
    manager.config = SimpleNamespace(sandbox_remote_workdir="/workspace")
    manager._preserve_line_endings = lambda _path, data: data
    monkeypatch.setattr(
        sandbox_module,
        "read_file_from_sandbox",
        lambda *_a, **_k: b"new\n",
    )

    stats = manager.sync_files_from_sandbox(
        SimpleNamespace(sandbox_id="sb"),
        tmp_path,
        ["run.sh"],
        expected_snapshots={"run.sh": expected},
    )

    assert target.read_text() == "new\n"
    assert os.stat(target).st_mode & 0o777 == 0o755
    assert stats["written_snapshots"]["run.sh"][2] == 0o755


def test_next_pullback_uses_latest_worker_output_not_rollback_baseline(tmp_path):
    sibling = tmp_path / "sibling.py"
    sibling.write_text("base\n")
    executor = _executor(tmp_path, FileScope(writable=["main.py"]))
    executor._remember_worker_discovered_baseline("sibling.py")
    executor._worker_discovered_paths.add("sibling.py")
    sibling.write_text("round-1\n")
    round_one = executor._worker_path_snapshot(sibling)
    executor._record_worker_discovered_outputs({"sibling.py": round_one})
    executor._pullback_written_snapshots["sibling.py"] = round_one

    assert executor._expected_pullback_snapshots()["sibling.py"] == round_one
    assert executor._worker_discovered_baselines["sibling.py"] != round_one


def test_bootstrap_wires_first_pullback_cas_for_declared_and_repair_paths(
    tmp_path, monkeypatch
):
    declared = tmp_path / "src" / "a.py"
    declared.parent.mkdir()
    declared.write_text("declared-base\n")
    manifest = tmp_path / "pom.xml"
    manifest.write_text("manifest-base\n")
    executor = _executor(tmp_path, FileScope(writable=["src/a.py"]))
    executor._build_manifest_files = lambda: ["pom.xml"]
    executor._sandbox = SimpleNamespace(sandbox_id="sb")

    class _Manager:
        def append_activity(self, *_args, **_kwargs):
            return None

        def sync_files_to_sandbox(self, _sandbox, _root, files, _remote):
            return {
                "uploaded": len(files),
                "files": list(files),
                "errors": [],
                "blocked_paths": [],
                "complete": True,
            }

    executor._sandbox_manager = _Manager()
    monkeypatch.setattr(
        "swarm.worker.executor_sync.get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")
        ),
    )

    asyncio.run(executor._sync_to_sandbox("bootstrap"))
    executor._repaired_extra_paths.add("pom.xml")
    expected = executor._expected_pullback_snapshots()

    assert expected["src/a.py"][1] == b"declared-base\n"
    assert expected["pom.xml"][1] == b"manifest-base\n"


def test_bootstrap_records_missing_create_baseline_even_without_upload(
    tmp_path, monkeypatch
):
    executor = _executor(tmp_path, FileScope(create_files=["src/new.py"]))
    executor._sandbox = SimpleNamespace(sandbox_id="sb")
    executor._sandbox_manager = SimpleNamespace(
        append_activity=lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "swarm.worker.executor_sync.get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")
        ),
    )

    asyncio.run(executor._sync_to_sandbox("bootstrap"))

    assert executor._expected_pullback_snapshots()["src/new.py"] == (
        "missing", None, None
    )


def test_post_transform_cas_rejects_concurrent_write(tmp_path):
    target = tmp_path / "A.java"
    target.write_text("import javax.servlet.Filter;\n")
    executor = _executor(tmp_path, FileScope(writable=["A.java"]))
    executor._pullback_written_snapshots["A.java"] = executor._worker_path_snapshot(target)
    target.write_text("// concurrent\n")

    with pytest.raises(Exception, match="concurrent change conflict"):
        executor._cas_transform_local_file(
            tmp_path, "A.java", lambda data: (data.replace(b"javax", b"jakarta"), None)
        )
    assert target.read_text() == "// concurrent\n"


def test_discovered_rollback_restores_file_mode(tmp_path):
    sibling = tmp_path / "script.sh"
    sibling.write_text("baseline\n")
    sibling.chmod(0o755)
    executor = _executor(tmp_path, FileScope(writable=["main.py"]))
    executor._remember_worker_discovered_baseline("script.sh")
    executor._worker_discovered_paths.add("script.sh")
    sibling.write_text("worker poison\n")
    sibling.chmod(0o600)
    executor._record_worker_discovered_outputs(
        {"script.sh": executor._worker_path_snapshot(sibling)}
    )

    assert executor._rollback_worker_discovered_paths() == []
    assert os.stat(sibling).st_mode & 0o777 == 0o755


def test_worker_with_enabled_sandbox_but_no_endpoint_fails_before_agent(
    tmp_path, monkeypatch
):
    executor = _executor(tmp_path, FileScope(writable=["a.py"]))
    created = False

    def _create_agent():
        nonlocal created
        created = True
        return {}

    executor._create_agent = _create_agent
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(use_for_worker=True, api_url="")
        ),
    )

    output = asyncio.run(executor._phase_prepare())

    assert output is not None and output.l1_passed is False
    assert output.l1_details.get("error") == "sandbox_config_invalid"
    assert created is False


def test_cleanup_failure_is_machine_readable_in_worker_output(tmp_path, monkeypatch):
    executor = _executor(tmp_path, FileScope(writable=["a.py"]))
    executor._worker_discovered_paths.add("sibling.py")
    monkeypatch.setattr(
        executor,
        "_rollback_worker_discovered_paths",
        lambda: ["sibling.py: restore failed"],
    )
    output = WorkerOutput(
        subtask_id="st-b4-root",
        diff="",
        summary="failed",
        confidence=Confidence.LOW,
        l1_passed=False,
        l1_details={"reason": "scope_violation"},
    )

    finalized = asyncio.run(executor._finalize_failed_worker_output(output))

    assert finalized.l1_passed is False
    assert finalized.l1_details["worker_discovered_cleanup_errors"] == [
        "sibling.py: restore failed"
    ]
    assert finalized.l1_details["pipeline_blocked"] == "worker_discovered_cleanup_failed"


def test_quarantined_workspace_blocks_next_worker_before_agent(tmp_path, monkeypatch):
    executor = _executor(tmp_path, FileScope(writable=["a.py"]))
    executor.project_id = "project-1"
    created = False

    async def _load(_project_id, _path):
        return {"errors": ["restore failed"]}

    def _create_agent():
        nonlocal created
        created = True
        return {}

    executor._create_agent = _create_agent
    monkeypatch.setattr(
        "swarm.worker.workspace_quarantine.load_workspace_quarantine", _load
    )
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=False, api_url="")),
    )
    output = asyncio.run(executor._phase_prepare())

    assert output is not None and output.l1_passed is False
    assert output.l1_details["error"] == "workspace_quarantined"
    assert created is False


def test_persistent_project_quarantine_blocks_worker_after_process_restart(
    tmp_path, monkeypatch
):
    executor = _executor(tmp_path, FileScope(writable=["a.py"]))
    executor.project_id = "project-1"
    monkeypatch.setattr(
        "swarm.project.store.get_worker_workspace_quarantine",
        lambda _project_id: {
            "active": True,
            "project_path": str(tmp_path.resolve()),
            "errors": ["persisted restore failure"],
        },
    )

    output = asyncio.run(executor._phase_prepare())

    assert output is not None
    assert output.l1_details["error"] == "workspace_quarantined"
    assert "persisted restore failure" in output.l1_details[
        "worker_discovered_cleanup_errors"
    ]


def test_cleanup_failure_persists_project_quarantine(tmp_path, monkeypatch):
    executor = _executor(tmp_path, FileScope(writable=["a.py"]))
    executor.project_id = "project-1"
    executor._worker_discovered_paths.add("sibling.py")
    monkeypatch.setattr(
        executor,
        "_rollback_worker_discovered_paths",
        lambda: ["sibling.py: restore failed"],
    )
    persisted = []
    monkeypatch.setattr(
        "swarm.project.store.set_worker_workspace_quarantine",
        lambda project_id, path, errors, **_kwargs: persisted.append(
            (project_id, path, errors)
        ),
    )
    output = WorkerOutput(
        subtask_id="st-b4-root",
        diff="",
        summary="failed",
        confidence=Confidence.LOW,
        l1_passed=False,
    )
    finalized = asyncio.run(executor._finalize_failed_worker_output(output))

    assert finalized.l1_passed is False
    assert persisted and persisted[0][0] == "project-1"


def test_failed_finalize_owns_cleanup_once_before_run_finally(tmp_path, monkeypatch):
    executor = _executor(tmp_path, FileScope(writable=["a.py"]))
    executor._worker_discovered_paths.add("sibling.py")
    calls = []
    monkeypatch.setattr(
        executor,
        "_rollback_worker_discovered_paths",
        lambda: calls.append("rollback") or ["restore failed"],
    )

    async def _early():
        return WorkerOutput(
            subtask_id="st-b4-root",
            diff="",
            summary="failed",
            confidence=Confidence.LOW,
            l1_passed=False,
        )

    monkeypatch.setattr(executor, "_phase_prepare", _early)
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=False)),
    )

    asyncio.run(executor.run())

    assert calls == ["rollback"]




def test_cancelled_worker_restores_discovered_file_before_exit(tmp_path, monkeypatch):
    sibling = tmp_path / "sibling.py"
    sibling.write_text("before\n")
    executor = _executor(tmp_path, FileScope(writable=["main.py"]))

    async def _cancel_after_pullback():
        executor._remember_worker_discovered_baseline("sibling.py")
        executor._worker_discovered_paths.add("sibling.py")
        sibling.write_text("poisoned\n")
        executor._record_worker_discovered_outputs(
            {"sibling.py": executor._worker_path_snapshot(sibling)}
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(executor, "_phase_prepare", _cancel_after_pullback)
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=False)),
    )

    try:
        asyncio.run(executor.run())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("取消必须向上传播")

    assert sibling.read_text() == "before\n"


def test_cancelled_worker_cleanup_failure_quarantines_workspace(tmp_path, monkeypatch):
    executor = _executor(tmp_path, FileScope(writable=["main.py"]))
    executor.project_id = "project-1"
    executor._worker_discovered_paths.add("sibling.py")
    monkeypatch.setattr(
        executor,
        "_rollback_worker_discovered_paths",
        lambda: ["sibling.py: restore failed"],
    )

    async def _cancel_after_pullback():
        raise asyncio.CancelledError

    monkeypatch.setattr(executor, "_phase_prepare", _cancel_after_pullback)
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=False)),
    )
    persisted = []
    monkeypatch.setattr(
        "swarm.project.store.set_worker_workspace_quarantine",
        lambda project_id, path, errors, **_kwargs: persisted.append(
            (project_id, path, errors)
        ),
    )
    try:
        asyncio.run(executor.run())
    except asyncio.CancelledError:
        pass
    assert persisted and persisted[0][0] == "project-1"


def test_repeated_cancel_still_releases_sandbox_resources(tmp_path, monkeypatch):
    from swarm.infra.cancellation import OwnedBlockingCancelled

    executor = _executor(tmp_path, FileScope(writable=["main.py"]))
    executor._worker_discovered_paths.add("sibling.py")
    killed = []

    async def _cancel():
        raise asyncio.CancelledError

    async def _second_cancel(*_args, **_kwargs):
        raise OwnedBlockingCancelled("success", result=[])

    monkeypatch.setattr(executor, "_phase_prepare", _cancel)
    monkeypatch.setattr(executor_module, "run_blocking_owned", _second_cancel)
    monkeypatch.setattr(executor, "kill_sandbox", lambda: killed.append(True))
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=False)),
    )

    try:
        asyncio.run(executor.run())
    except asyncio.CancelledError:
        pass

    assert killed == [True]


@pytest.mark.asyncio
async def test_first_cancel_during_tool_drain_propagates_after_cleanup(
    tmp_path, monkeypatch
):
    """正常 return 已进 finally 后首次取消：先排空并清资源，再传播取消，
    不得把 task.cancelling()>0 的任务伪装成正常 WorkerOutput。"""
    from swarm.infra.cancellation import OwnedBlockingCancelled

    executor = _executor(tmp_path, FileScope(writable=["main.py"]))
    entered_drain = asyncio.Event()
    release_drain = asyncio.Event()
    killed = []

    async def _early_output():
        return WorkerOutput(
            subtask_id=executor.subtask.id,
            diff="",
            summary="early",
            confidence=Confidence.LOW,
            l1_passed=False,
        )

    async def _owned(func, *_args, operation=None, **_kwargs):
        if operation == "Worker 副作用 Tool 线程排空":
            entered_drain.set()
            try:
                await release_drain.wait()
            except asyncio.CancelledError:
                release_drain.set()
                func(*_args)
                raise OwnedBlockingCancelled("success") from None
        return func(*_args)

    monkeypatch.setattr(executor, "_phase_prepare", _early_output)
    monkeypatch.setattr(executor_module, "run_blocking_owned", _owned)
    monkeypatch.setattr(executor, "kill_sandbox", lambda: killed.append(True))
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=False)),
    )

    run_task = asyncio.create_task(executor.run())
    await asyncio.wait_for(entered_drain.wait(), timeout=1)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert killed == [True]






@pytest.mark.asyncio
async def test_sync_from_sandbox_passes_live_expected_snapshots_to_manager(
    tmp_path, monkeypatch
):
    """首次 pull-back 生产调用点必须将 bootstrap 精确快照传给 manager CAS。"""
    target = tmp_path / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("base\n")
    executor = _executor(tmp_path, FileScope(writable=["src/main.py"]))
    executor._sandbox = SimpleNamespace(sandbox_id="sb")
    executor._bootstrap_marker = ".swarm-bootstrap-marker"
    baseline = executor._worker_path_snapshot(target)
    executor._bootstrap_entry_snapshots = {"src/main.py": baseline}
    captured = {}

    class _Manager:
        def append_activity(self, *_args, **_kwargs):
            return None

        def sync_files_from_sandbox(self, *_args, **kwargs):
            captured.update(kwargs)
            return {
                "contents": {"src/main.py": "base\n"},
                "written_snapshots": {"src/main.py": baseline},
                "conflicts": [],
                "errors": [],
                "skipped": 0,
                "downloaded": 1,
            }

    executor._sandbox_manager = _Manager()
    monkeypatch.setattr(executor, "_list_sandbox_files_under", lambda _dirs: [])
    monkeypatch.setattr(executor, "_list_sandbox_modified_files", lambda _marker: [])
    monkeypatch.setattr(executor, "_context_sibling_rels", lambda _root: set())
    monkeypatch.setattr(executor, "_enforce_baseline_anchor_integrity", lambda *_a: None)
    monkeypatch.setattr(executor, "_normalize_jvm_namespace", lambda *_a: asyncio.sleep(0))
    monkeypatch.setattr(
        "swarm.worker.executor_sync.get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")
        ),
    )

    await executor._sync_from_sandbox("wire")

    assert captured["expected_snapshots"] == {"src/main.py": baseline}
