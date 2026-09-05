"""Worker 删除生命周期、回滚与远端删除传播回归。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm.types import Confidence, FileScope, SubTask, WorkerOutput
from swarm.worker.executor import WorkerExecutor


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


def test_delete_target_is_seeded_and_local_fallback_emits_deletion_diff(tmp_path):
    target = tmp_path / "old.py"
    target.write_text("old\n")
    executor = _executor(tmp_path, FileScope(delete_files=["old.py"]))

    assert "old.py" in executor._scope_files()
    executor._pre_sync_contents = executor._snapshot_scope_local(tmp_path)
    target.unlink()
    executor._post_sync_contents = executor._snapshot_scope_local(
        tmp_path, files=executor._change_files()
    )

    diff = executor._get_git_diff()
    assert "--- a/old.py" in diff
    assert "+++ /dev/null" in diff


def test_remote_declared_deletion_pullback_is_idempotent_within_executor(tmp_path):
    target = tmp_path / "old.py"
    target.write_text("old\n")
    executor = _executor(tmp_path, FileScope(delete_files=["old.py"]))
    executor._snapshot_declared_delete_seeds(tmp_path)

    assert executor._apply_local_deletions(tmp_path, lambda _rel: False) == [
        "old.py"
    ]
    assert executor._apply_local_deletions(tmp_path, lambda _rel: False) == []
    assert executor._deleted_local_paths == {"old.py"}


def test_remote_declared_deletion_unlink_failure_is_transient(tmp_path, monkeypatch):
    from swarm.models.errors import TransientInfraError

    target = tmp_path / "old.py"
    target.write_text("old\n")
    executor = _executor(tmp_path, FileScope(delete_files=["old.py"]))
    executor._snapshot_declared_delete_seeds(tmp_path)
    original_unlink = Path.unlink

    def _deny_unlink(path, *args, **kwargs):
        if path == target:
            raise PermissionError("denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _deny_unlink)

    with pytest.raises(TransientInfraError, match="old.py"):
        executor._apply_local_deletions(tmp_path, lambda _rel: False)
    assert target.exists()


def test_non_git_empty_and_binary_deletions_are_applyable(tmp_path):
    if not shutil.which("git"):
        return
    for name, content in (("empty.bin", b""), ("binary.bin", b"abc\x00def")):
        root = tmp_path / name
        root.mkdir()
        target = root / name
        target.write_bytes(content)
        executor = _executor(root, FileScope(delete_files=[name]))
        executor._pre_sync_contents = executor._snapshot_scope_local(root)
        target.unlink()
        executor._post_sync_contents = executor._snapshot_scope_local(
            root, files=executor._change_files()
        )

        diff = executor._get_git_diff()
        assert diff != "(无变更)"
        target.write_bytes(content)
        applied = subprocess.run(
            ["git", "apply", "--unsafe-paths", "-"],
            cwd=root,
            input=diff,
            text=True,
            capture_output=True,
        )
        assert applied.returncode == 0, applied.stderr
        assert not target.exists()


def test_non_git_symlink_deletion_patch_unlinks_only_link(tmp_path):
    if not shutil.which("git"):
        return
    outside = tmp_path.parent / f"{tmp_path.name}-target.txt"
    outside.write_text("keep\n")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    executor = _executor(tmp_path, FileScope(delete_files=["link.txt"]))
    executor._pre_sync_contents = executor._snapshot_scope_local(tmp_path)
    link.unlink()
    executor._post_sync_contents = executor._snapshot_scope_local(
        tmp_path, files=executor._change_files()
    )

    diff = executor._get_git_diff()
    link.symlink_to(outside)
    applied = subprocess.run(
        ["git", "apply", "--unsafe-paths", "-"],
        cwd=tmp_path,
        input=diff,
        text=True,
        capture_output=True,
    )

    assert applied.returncode == 0, applied.stderr
    assert not link.is_symlink()
    assert outside.read_text() == "keep\n"


def test_failed_non_git_declared_deletion_is_restored_before_next_run(tmp_path):
    target = tmp_path / "old.py"
    target.write_text("before\n")
    executor = _executor(tmp_path, FileScope(delete_files=["old.py"]))
    executor._delete_seed_snapshots["old.py"] = executor._worker_path_snapshot(
        target
    )
    target.unlink()
    executor._deleted_local_paths.add("old.py")
    output = WorkerOutput(
        subtask_id="st-b4-root",
        diff="",
        summary="failed",
        confidence=Confidence.LOW,
        l1_passed=False,
    )

    finalized = asyncio.run(executor._finalize_failed_worker_output(output))

    assert finalized.l1_passed is False
    assert target.read_text() == "before\n"
    assert executor._deleted_local_paths == set()


def test_failed_deletion_cleanup_preserves_concurrent_recreation(tmp_path):
    target = tmp_path / "old.py"
    target.write_text("before\n")
    executor = _executor(tmp_path, FileScope(delete_files=["old.py"]))
    executor._delete_seed_snapshots["old.py"] = executor._worker_path_snapshot(
        target
    )
    target.unlink()
    executor._deleted_local_paths.add("old.py")
    target.write_text("concurrent\n")
    output = WorkerOutput(
        subtask_id="st-b4-root",
        diff="",
        summary="failed",
        confidence=Confidence.LOW,
        l1_passed=False,
    )

    finalized = asyncio.run(executor._finalize_failed_worker_output(output))

    assert target.read_text() == "concurrent\n"
    errors = finalized.l1_details["worker_discovered_cleanup_errors"]
    assert any("[CONCURRENT_CHANGE]" in error for error in errors)


def test_local_delete_tool_is_journaled_and_rolled_back_on_failed_output(tmp_path):
    from swarm.tools.file_tools import delete_file
    from swarm.tools.inflight import (
        clear_tool_inflight_tracker,
        set_tool_inflight_tracker,
    )
    from swarm.tools.paths import set_workspace_root
    from swarm.tools.scope_guard import clear_scope, set_scope

    target = tmp_path / "old.py"
    target.write_text("before\n")
    executor = _executor(tmp_path, FileScope(delete_files=["old.py"]))
    executor._snapshot_declared_delete_seeds(tmp_path)
    set_workspace_root(str(tmp_path))
    set_scope(executor.effective_scope)
    set_tool_inflight_tracker()
    try:
        assert delete_file.invoke({"path": "old.py"}).startswith("✅")
        assert not target.exists()
        output = WorkerOutput(
            subtask_id="st-b4-root",
            diff="",
            summary="failed",
            confidence=Confidence.LOW,
            l1_passed=False,
        )

        finalized = asyncio.run(executor._finalize_failed_worker_output(output))

        assert finalized.l1_passed is False
        assert target.read_text() == "before\n"
    finally:
        clear_tool_inflight_tracker()
        clear_scope()
        set_workspace_root(None)

def test_failed_finalize_drains_parallel_deletes_before_single_rollback(
    tmp_path, monkeypatch
):
    import contextvars
    import threading

    from swarm.tools.inflight import (
        record_local_tool_deletion,
        track_tool_side_effect,
    )

    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("a\n")
    second.write_text("b\n")
    executor = _executor(
        tmp_path, FileScope(delete_files=["a.py", "b.py"])
    )
    executor._snapshot_declared_delete_seeds(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    worker_thread = None

    def _late_delete():
        with track_tool_side_effect():
            entered.set()
            release.wait(5)
            second.unlink()
            record_local_tool_deletion("b.py")

    async def _failed_with_parallel_deletes():
        nonlocal worker_thread
        with track_tool_side_effect():
            first.unlink()
            record_local_tool_deletion("a.py")
        copied = contextvars.copy_context()
        worker_thread = threading.Thread(target=copied.run, args=(_late_delete,))
        worker_thread.start()
        assert await asyncio.to_thread(entered.wait, 2)
        threading.Timer(0.1, release.set).start()
        return WorkerOutput(
            subtask_id=executor.subtask.id,
            diff="",
            summary="failed",
            confidence=Confidence.LOW,
            l1_passed=False,
        )

    monkeypatch.setattr(executor, "_phase_prepare", _failed_with_parallel_deletes)
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=False)),
    )
    try:
        asyncio.run(executor.run())
    finally:
        release.set()
        if worker_thread is not None:
            worker_thread.join(timeout=2)

    assert first.read_text() == "a\n"
    assert second.read_text() == "b\n"
    assert executor._deleted_local_paths == set()

def test_delete_propagation_unlinks_leaf_symlink_without_following_target(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("keep\n")
    link = tmp_path / "outside-link"
    link.symlink_to(outside)
    executor = _executor(tmp_path, FileScope(delete_files=["outside-link"]))
    executor._delete_seed_snapshots["outside-link"] = executor._worker_path_snapshot(
        link
    )

    deleted = executor._apply_local_deletions(tmp_path, lambda _rel: False)

    assert deleted == ["outside-link"]
    assert not link.is_symlink()
    assert outside.read_text() == "keep\n"


def test_remote_delete_seed_uses_placeholder_for_symlink_without_reading_target(
    tmp_path, monkeypatch
):
    outside = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    outside.write_text("must-not-upload\n")
    link = tmp_path / "outside-link"
    link.symlink_to(outside)
    executor = _executor(tmp_path, FileScope(delete_files=["outside-link"]))
    executor._sandbox = SimpleNamespace(sandbox_id="sb")
    captured = {}

    class _Manager:
        def append_activity(self, *_args, **_kwargs):
            return None

        def sync_files_to_sandbox(self, _sandbox, root, files, _remote):
            captured.update({rel: (root / rel).read_bytes() for rel in files})
            return {
                "uploaded": len(files), "files": list(files), "errors": [],
                "blocked_paths": [], "complete": True,
            }

    executor._sandbox_manager = _Manager()
    monkeypatch.setattr(
        "swarm.worker.executor_sync.get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")
        ),
    )

    asyncio.run(executor._sync_to_sandbox("bootstrap"))

    assert captured == {"outside-link": b""}
    assert outside.read_text() == "must-not-upload\n"
    assert executor._apply_local_deletions(tmp_path, lambda _rel: False) == [
        "outside-link"
    ]
    assert outside.exists()

@pytest.mark.asyncio
async def test_verify_and_produce_pullbacks_keep_remote_deletion_idempotent(
    tmp_path, monkeypatch
):
    target = tmp_path / "old.py"
    target.write_text("old\n")
    executor = _executor(tmp_path, FileScope(delete_files=["old.py"]))
    executor._snapshot_declared_delete_seeds(tmp_path)
    executor._sandbox = SimpleNamespace(sandbox_id="sb")
    executor._bootstrap_marker = ".swarm-bootstrap-marker"

    class _Manager:
        def append_activity(self, *_args, **_kwargs):
            return None

        def run_command(self, *_args, **_kwargs):
            return SimpleNamespace(success=True, error=None, stdout="__N__\n")

    executor._sandbox_manager = _Manager()
    monkeypatch.setattr(executor, "_list_sandbox_modified_files", lambda _m: [])
    monkeypatch.setattr(executor, "_context_sibling_rels", lambda _root: set())

    await executor._sync_from_sandbox("verify")
    await executor._sync_from_sandbox("produce")

    assert not target.exists()
    assert executor._deleted_local_paths == {"old.py"}
