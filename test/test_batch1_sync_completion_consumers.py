"""Batch 1：同步安全阻断必须被 Worker/L2/Runtime 的真实消费入口 fail-closed。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskHarness
from swarm.worker.executor import WorkerExecutor


_INCOMPLETE = {
    "uploaded": 0,
    "errors": [],
    "complete": False,
    "blocked_paths": [{"path": "secret.py", "reason": "secret_content:Private Key"}],
}


class _Sandbox:
    sandbox_id = "sb-incomplete"


class _IncompleteManager:
    def __init__(self):
        self.commands: list[str] = []
        self.killed: list[str] = []
        self._instances = {}

    def create(self, **kwargs):
        return _Sandbox()

    def sync_project_to_sandbox(self, *args, **kwargs):
        return dict(_INCOMPLETE)

    def run_command(self, sandbox, command, timeout=120, **kwargs):
        self.commands.append(command)
        return SimpleNamespace(stdout="__RC__0", stderr="", error=None)

    def kill(self, sandbox_id):
        self.killed.append(sandbox_id)

    def try_extend_lifetime(self, sandbox, seconds):
        return True


def test_worker_targeted_sync_records_security_block_for_l1(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "secret.py").write_text("print('x')\n", encoding="utf-8")
    subtask = SubTask(
        id="st-security",
        description="安全同步消费",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["secret.py"]),
        harness=TaskHarness(language="python"),
    )
    executor = WorkerExecutor(subtask=subtask, project_path=str(project))
    executor._sandbox = object()
    executor._sandbox_manager = SimpleNamespace(
        sync_files_to_sandbox=lambda *args, **kwargs: dict(_INCOMPLETE),
    )
    executor._sandbox_has_source = False
    executor._scope_files = lambda: ["secret.py"]
    executor._log = lambda *args, **kwargs: None
    monkeypatch.setenv("SWARM_WORKER_CLEAN_UPLOAD", "false")
    monkeypatch.setattr(
        "swarm.worker.executor_sync.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")),
    )

    asyncio.run(executor._sync_to_sandbox("bootstrap"))

    assert executor._upload_blocked_rels == _INCOMPLETE["blocked_paths"]


def test_worker_targeted_sync_missing_complete_contract_is_not_treated_as_clean(
    tmp_path, monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    subtask = SubTask(
        id="st-incomplete-contract",
        description="同步完整性契约",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["main.py"]),
        harness=TaskHarness(language="python"),
    )
    executor = WorkerExecutor(subtask=subtask, project_path=str(project))
    executor._sandbox = object()
    executor._sandbox_manager = SimpleNamespace(
        sync_files_to_sandbox=lambda *args, **kwargs: {
            "uploaded": 1, "errors": [], "blocked_paths": [], "files": ["main.py"],
        },
    )
    executor._sandbox_has_source = False
    executor._scope_files = lambda: ["main.py"]
    executor._log = lambda *args, **kwargs: None
    monkeypatch.setenv("SWARM_WORKER_CLEAN_UPLOAD", "false")
    monkeypatch.setattr(
        "swarm.worker.executor_sync.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")),
    )

    asyncio.run(executor._sync_to_sandbox("bootstrap"))

    assert executor._upload_error_rels == ["同步完整性未确认: complete != true"]


def test_l2_functional_does_not_run_after_incomplete_sync():
    from swarm.brain.nodes import _run_l2_in_sandbox

    manager = _IncompleteManager()
    with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=manager), \
         patch("swarm.worker.sandbox.write_file_to_sandbox"):
        result = _run_l2_in_sandbox(
            "/tmp/project", "diff --git a/x b/x\n", "pytest", project_id="p1",
        )

    assert result is None
    assert manager.commands == []


def test_l2_functional_continues_after_explicit_credential_path_exclusion():
    from swarm.brain.nodes import _run_l2_in_sandbox

    manager = _IncompleteManager()
    manager.sync_project_to_sandbox = lambda *args, **kwargs: {
        "uploaded": 1,
        "errors": [],
        "complete": True,
        "blocked_paths": [],
        "excluded_paths": [
            {"path": ".env", "reason": "sensitive_filename"},
        ],
    }

    def run_command(_sandbox, command, **_kwargs):
        manager.commands.append(command)
        marker = "__APPLY_RC__0" if "git apply" in command else "__RC__0"
        return SimpleNamespace(stdout=marker, stderr="", error=None)

    manager.run_command = run_command
    with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=manager), \
         patch("swarm.worker.sandbox.write_file_to_sandbox"):
        result = _run_l2_in_sandbox(
            "/tmp/project", "diff --git a/x b/x\n", "pytest", project_id="p1",
        )

    assert result is True
    assert len(manager.commands) == 2


def test_l2_compile_does_not_run_after_incomplete_sync(monkeypatch):
    from swarm.brain import nodes

    manager = _IncompleteManager()
    monkeypatch.setattr(nodes, "_sandbox_available", lambda: True)
    monkeypatch.setattr("swarm.worker.sandbox.get_sandbox_manager", lambda: manager)

    result = nodes._run_reactor_build_in_sandbox("/tmp/project", "", "pytest")

    assert result == (False, False, "", None)
    assert manager.commands == []


def test_runtime_smoke_does_not_build_after_incomplete_sync(monkeypatch, tmp_path):
    from swarm.brain.nodes import verify

    manager = _IncompleteManager()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("swarm.brain.nodes._sandbox_available", lambda: True)
    monkeypatch.setattr("swarm.worker.sandbox.get_sandbox_manager", lambda: manager)

    sandbox, reason, details = verify._acquire_smoke_sandbox(
        manager, "", "p1", str(project), 900,
    )

    assert sandbox is None
    assert reason == "rebuild_failed"
    assert "同步不完整" in details["rebuild_error"]
    assert manager.commands == []


def _run_l1_with_manifest_push_result(tmp_path, monkeypatch, sync_result):
    """从 L1 公共入口驱动 module-registration 清单推进的完整消费链。"""
    from swarm.worker import l1_pipeline
    from swarm.worker import workspace_manifest

    (tmp_path / "hello.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n", encoding="utf-8",
    )
    subtask = SubTask(
        id="st-manifest-sync",
        description="清单推进完整性消费",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["hello.py"]),
        harness=TaskHarness(
            language="python", build_command="python -m compileall -q .",
        ),
    )
    diff = (
        "--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n"
        "-print('old')\n+print('ok')\n"
    )

    class _Manager:
        def sync_files_to_sandbox(self, *args, **kwargs):
            return dict(sync_result)

    monkeypatch.setattr(l1_pipeline, "_compile_files", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(
        workspace_manifest,
        "reconcile_workspace_manifests",
        lambda *a, **k: {
            "modified_manifests": ["pyproject.toml"],
            "added": {"pyproject.toml": ["demo"]},
            "reconcile_errors": {},
        },
    )
    monkeypatch.setattr(
        l1_pipeline, "_sandbox_ctx", lambda: (object(), _Manager(), "/workspace"),
    )
    monkeypatch.setenv("SWARM_WORKER_L1_LINT", "false")
    monkeypatch.setenv("SWARM_WORKER_L1_FORMAT", "false")

    return l1_pipeline.run_l1_pipeline(str(tmp_path), subtask, diff, timeout=30)


def test_l1_manifest_security_block_stops_before_build(tmp_path, monkeypatch):
    """清单内容被确定性安全闸拒绝时，生产 L1 入口不得继续 build。"""
    ok, details = _run_l1_with_manifest_push_result(
        tmp_path,
        monkeypatch,
        {
            "uploaded": 0,
            "errors": [],
            "complete": False,
            "blocked_paths": [
                {"path": "pyproject.toml", "reason": "secret_content:Private Key"},
            ],
        },
    )

    assert ok is False
    assert details["reason"] == "module_registration_push_security_blocked"
    assert details["pipeline_blocked"] == "module_registration_push_security_blocked"
    assert details["module_registration_push_undelivered"]["failure_kind"] == (
        "deterministic_security"
    )
    assert "build_command" not in details


def test_l1_manifest_guard_outage_is_transient_and_stops_before_build(
    tmp_path, monkeypatch,
):
    """扫描器不可用属于可重试阻断，但同样不能让旧清单继续参与 build。"""
    ok, details = _run_l1_with_manifest_push_result(
        tmp_path,
        monkeypatch,
        {
            "uploaded": 0,
            "errors": [],
            "complete": False,
            "blocked_paths": [
                {"path": "pyproject.toml", "reason": "content_guard_unavailable"},
            ],
        },
    )

    assert ok is True
    assert details["pipeline_blocked"] == "module_registration_push_transient"
    assert details["not_run_kind"] == "blocked"
    assert details["module_registration_push_undelivered"]["failure_kind"] == "transient"
    assert "build_command" not in details
