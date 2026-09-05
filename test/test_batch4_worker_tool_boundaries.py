"""批次 4A：Worker 工具、仓库控制面与 FileScope 授权边界。"""

from __future__ import annotations

import asyncio
import contextvars
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.project.diff_apply import files_from_unified_diff
from swarm.tools import build_tools, file_tools
from swarm.tools.build_tools import clear_sandbox_context, set_sandbox_context
from swarm.tools.file_tools import write_file
from swarm.tools.git_tools import git_diff, git_log
from swarm.tools.paths import set_workspace_root
from swarm.tools.scope_guard import clear_scope, require_writable, set_scope
from swarm.types import Confidence, FileScope, SubTask, WorkerOutput
from swarm.worker.agent import _get_worker_tools
from swarm.worker.l1_pipeline import (
    _compile_files,
    _run_check_split,
    _run_l1_command,
    _scope_violations,
    _lint_python,
    _build_error_is_upstream,
)
from swarm.worker.format_gate import format_files


@pytest.fixture(autouse=True)
def _clear_worker_context():
    from swarm.tools.inflight import clear_tool_inflight_tracker

    original_workspace = os.environ.get("SWARM_WORKSPACE_ROOT")
    previous_isolation = build_tools.worker_command_isolation_required()
    clear_sandbox_context()
    clear_tool_inflight_tracker()
    clear_scope()
    set_workspace_root(None)
    if original_workspace is None:
        os.environ.pop("SWARM_WORKSPACE_ROOT", None)
    else:
        os.environ["SWARM_WORKSPACE_ROOT"] = original_workspace
    yield
    clear_sandbox_context()
    build_tools.set_worker_command_isolation(previous_isolation)
    clear_tool_inflight_tracker()
    clear_scope()
    set_workspace_root(None)
    if original_workspace is None:
        os.environ.pop("SWARM_WORKSPACE_ROOT", None)
    else:
        os.environ["SWARM_WORKSPACE_ROOT"] = original_workspace


def test_local_run_command_refuses_interpreter_execution(monkeypatch):
    """无远程沙箱时，LLM 的自由命令不得落到宿主 subprocess。"""
    monkeypatch.setattr(
        build_tools,
        "_worker_config",
        lambda: SimpleNamespace(command_whitelist=["python"], max_execution_time=120),
    )
    monkeypatch.setattr(
        "swarm.config.command_blacklist_store.check_command_hardened",
        lambda _command: (True, ""),
    )

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("本地自由命令越过隔离边界")

    monkeypatch.setattr(build_tools.subprocess, "run", _must_not_run)
    result = build_tools.run_command.invoke(
        {"command": "python -c __import__('os').environ.copy()"}
    )

    assert "LOCAL_COMMAND_DISABLED" in result
    assert "未执行" in result


def test_remote_sandbox_run_command_remains_available(monkeypatch):
    """拒绝仅针对宿主执行；活跃远程沙箱仍可运行已授权命令。"""
    monkeypatch.setattr(
        build_tools,
        "_worker_config",
        lambda: SimpleNamespace(command_whitelist=["python"], max_execution_time=120),
    )
    sandbox = MagicMock()
    manager = MagicMock()
    manager.run_command.return_value = SimpleNamespace(
        success=True,
        error="",
        stdout="sandbox-ok",
        stderr="",
    )
    set_sandbox_context(sandbox, manager)

    result = build_tools.run_command.invoke({"command": "python -m pytest"})

    assert result.startswith("✅")
    manager.run_command.assert_called_once()


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (build_tools.run_compile, {"language": "python", "target": "payload.py"}),
        (build_tools.run_tests, {"language": "pytest", "test_filter": "payload.py"}),
    ],
)
def test_local_structured_command_tools_do_not_execute_project_code(monkeypatch, tool, kwargs):
    """结构化编译/测试工具与自由命令共用远程沙箱硬闸。"""
    monkeypatch.setattr(
        build_tools,
        "_worker_config",
        lambda: SimpleNamespace(
            command_whitelist=["python -m py_compile", "python -m pytest"],
            max_execution_time=120,
        ),
    )

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("结构化命令落到宿主执行")

    monkeypatch.setattr(build_tools, "_run", _must_not_run)
    result = tool.invoke(kwargs)

    assert "LOCAL_COMMAND_DISABLED" in result
    assert "未执行" in result


@pytest.mark.parametrize("runner", [_run_l1_command, _run_check_split])
def test_worker_l1_context_loss_never_falls_back_to_host_shell(monkeypatch, tmp_path, runner):
    """Worker 生命周期内即使沙箱 ContextVar 丢失，L1 也必须 fail-closed。"""
    assert hasattr(build_tools, "set_worker_command_isolation")
    build_tools.set_worker_command_isolation(True)

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("L1 隔离上下文丢失后落到宿主 shell")

    monkeypatch.setattr("swarm.worker.l1_pipeline.subprocess.run", _must_not_run)
    try:
        result = runner("python payload.py", str(tmp_path))
    finally:
        build_tools.clear_worker_command_isolation()

    assert result[0] == 126
    assert "WORKER_SANDBOX_REQUIRED" in " ".join(map(str, result))


def test_worker_file_context_loss_never_falls_back_to_host_tree(tmp_path):
    target = tmp_path / "owned.py"
    target.write_text("keep\n")
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(writable=["owned.py"], delete_files=["owned.py"]))
    build_tools.set_worker_command_isolation(True)
    clear_sandbox_context()
    try:
        write_result = file_tools.write_file.invoke(
            {"path": "owned.py", "content": "poison\n"}
        )
        delete_result = file_tools.delete_file.invoke({"path": "owned.py"})
    finally:
        build_tools.clear_worker_command_isolation()

    assert "WORKER_SANDBOX_REQUIRED" in write_result
    assert "WORKER_SANDBOX_REQUIRED" in delete_result
    assert target.read_text() == "keep\n"


def test_executor_drains_cancelled_sync_file_tool_before_cleanup(
    tmp_path, monkeypatch
):
    from swarm.worker.executor import WorkerExecutor

    target = tmp_path / "late.py"
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(writable=["late.py"]))
    subtask = SubTask(
        id="st-tool-drain",
        description="x",
        scope=FileScope(writable=["late.py"]),
    )
    executor = WorkerExecutor(subtask, project_path=str(tmp_path))
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    original_write_text = Path.write_text

    def _blocking_write(path_obj, *args, **kwargs):
        if path_obj == target:
            started.set()
            release.wait(5)
            result = original_write_text(path_obj, *args, **kwargs)
            completed.set()
            return result
        return original_write_text(path_obj, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _blocking_write)
    cleanup_observations = []
    monkeypatch.setattr(
        executor,
        "kill_sandbox",
        lambda: cleanup_observations.append(completed.is_set()),
    )
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=False)),
    )

    async def _phase_prepare():
        invocation = asyncio.create_task(
            file_tools.write_file.ainvoke({"path": "late.py", "content": "done\n"})
        )
        assert await asyncio.to_thread(started.wait, 5), "同步 Tool 必须已进入受租约写区"
        invocation.cancel()
        try:
            await invocation
        except asyncio.CancelledError:
            pass
        threading.Timer(0.1, release.set).start()
        return WorkerOutput(
            subtask_id=subtask.id,
            diff="",
            summary="cancelled tool",
            confidence=Confidence.LOW,
            l1_passed=False,
        )

    monkeypatch.setattr(executor, "_phase_prepare", _phase_prepare)

    asyncio.run(executor.run())

    assert completed.is_set()
    assert cleanup_observations == [True]
    assert target.read_text() == "done\n"


def test_tracker_close_rejects_thread_blocked_before_lease(tmp_path):
    """关门与计数必须同一把锁原子裁决：收尾前尚未取得 lease 的
    迟到线程，不得在 close_and_wait 看到 count=0 返回后再进入写区。"""
    from concurrent.futures import ThreadPoolExecutor

    from swarm.tools.inflight import (
        clear_tool_inflight_tracker,
        close_and_wait_for_tool_side_effects,
        set_tool_inflight_tracker,
        track_tool_side_effect,
    )

    target = tmp_path / "too-late.py"
    before_lease = threading.Event()
    release = threading.Event()
    tracker = set_tool_inflight_tracker()
    copied = contextvars.copy_context()

    def _late_writer():
        before_lease.set()
        release.wait(2)
        with track_tool_side_effect():
            target.write_text("poison\n")

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copied.run, _late_writer)
            assert before_lease.wait(1)
            close_and_wait_for_tool_side_effects(tracker)
            release.set()
            with pytest.raises(RuntimeError, match="WORKER_TOOL_LIFECYCLE_CLOSED"):
                future.result(timeout=2)
    finally:
        release.set()
        clear_tool_inflight_tracker()

    assert not target.exists()


@pytest.mark.asyncio
async def test_agent_timeout_closes_tool_gate_before_l1(tmp_path):
    """Agent 超时返回前就封门；迟到 Tool 不能越过后续 L1/diff 再写。"""
    from swarm.tools.inflight import (
        clear_tool_inflight_tracker,
        set_tool_inflight_tracker,
        track_tool_side_effect,
    )
    from swarm.worker.executor_agent import _AgentLoopMixin

    target = tmp_path / "after-timeout.py"
    before_lease = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    rejected: list[str] = []

    def _late_writer():
        before_lease.set()
        release.wait(2)
        try:
            with track_tool_side_effect():
                target.write_text("poison\n")
        except RuntimeError as exc:
            rejected.append(str(exc))
        finally:
            finished.set()

    class _TimeoutAgent:
        async def ainvoke(self, *_args, **_kwargs):
            copied = contextvars.copy_context()
            threading.Thread(target=copied.run, args=(_late_writer,), daemon=True).start()
            assert await asyncio.to_thread(before_lease.wait, 1)
            raise asyncio.TimeoutError

    class _Host(_AgentLoopMixin):
        def __init__(self):
            self._agent = {"agent": _TimeoutAgent()}
            self.start_time = time.monotonic()
            self.max_execution_time = 60
            self.max_iterations = 2
            self.subtask = SimpleNamespace(
                id="st-timeout-gate", difficulty=SimpleNamespace(value="medium")
            )
            self.project_id = "p"
            self.task_id = "t"
            self.phase = SimpleNamespace(value="coding")

        def _log(self, _message):
            return None

    set_tool_inflight_tracker()
    try:
        result = await _Host()._run_agent("x", step="code")
        assert "Agent 调用超时" in result
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
    finally:
        release.set()
        clear_tool_inflight_tracker()

    assert rejected and "WORKER_TOOL_LIFECYCLE_CLOSED" in rejected[0]
    assert not target.exists()


def test_fresh_context_is_fail_closed_for_per_file_compile(monkeypatch, tmp_path):
    """全新的 Context 必须继承安全缺省，逐文件编译也不得旁路到宿主机。"""
    (tmp_path / "payload.py").write_text("x = 1\n")

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("逐文件编译落到宿主 subprocess")

    monkeypatch.setattr("swarm.worker.l1_pipeline.subprocess.run", _must_not_run)
    result = contextvars.Context().run(
        _compile_files, str(tmp_path), ["payload.py"]
    )

    assert result[0] is False
    assert "WORKER_SANDBOX_REQUIRED" in result[1]


def test_fresh_context_format_gate_never_runs_host_formatter(monkeypatch, tmp_path):
    (tmp_path / "payload.py").write_text("x=1\n")
    monkeypatch.setattr("swarm.worker.format_gate._which", lambda _name: "/usr/bin/ruff")

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("格式化器落到宿主 subprocess")

    monkeypatch.setattr(build_tools.subprocess, "run", _must_not_run)
    result = contextvars.Context().run(
        format_files, str(tmp_path), ["payload.py"]
    )

    assert result["status"] == "skipped"
    assert result["formatted"] == []


def test_executor_lifecycle_sets_and_clears_command_isolation(monkeypatch, tmp_path):
    """隔离标志必须由真实 Worker 生命周期接线，而不是只存在于工具单测。"""
    from swarm.worker.executor import WorkerExecutor

    subtask = SubTask(id="st-isolation", description="x", scope=FileScope(writable=["a.py"]))
    executor = WorkerExecutor(subtask, project_path=str(tmp_path))

    async def _early():
        assert build_tools.worker_command_isolation_required() is True
        return WorkerOutput(
            subtask_id=subtask.id,
            diff="",
            summary="early",
            confidence=Confidence.LOW,
            l1_passed=False,
        )

    monkeypatch.setattr(executor, "_phase_prepare", _early)
    monkeypatch.setattr(
        "swarm.worker.executor.get_config",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(use_for_worker=True)),
    )

    asyncio.run(executor.run())

    # asyncio.run 在复制的 Context 中清理；测试外层仍保留 conftest 的显式本地授权。
    assert build_tools.worker_command_isolation_required() is False


def test_worker_toolset_has_no_repository_subprocess_surface():
    """LLM 子任务不直接拥有宿主仓库的 checkout/diff/log/blame 控制面。"""
    names = {tool.name for tool in _get_worker_tools()}
    assert {"git_checkout", "git_diff", "git_log", "git_blame"}.isdisjoint(names)
    assert {"delete_file", "run_compile", "run_tests"}.issubset(names)


def test_git_diff_rejects_option_target_without_host_write(tmp_path):
    """即便被可信调用方直接调用，target 也不能重新解释成 Git 选项。"""
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(readable=["src/a.py"]))
    output = tmp_path / "host-write.txt"

    result = git_diff.invoke({"target": f"--output={output}", "path": "src/a.py"})

    assert "拒绝" in result
    assert not output.exists()


@pytest.mark.parametrize("tool", [git_diff, git_log])
def test_git_read_tools_require_an_explicit_scoped_path(tool):
    set_scope(FileScope(readable=["src/a.py"]))
    result = tool.invoke({})
    assert "GIT_PATH_REQUIRED" in result


@pytest.mark.parametrize(
    ("candidate", "allowed"),
    [
        ("src/a.py", True),
        ("src/sub/b.py", True),
        ("evil/src/a.py", False),
        ("src/../secret.py", False),
        ("/workspace/src/a.py", False),
    ],
)
def test_filescope_matches_only_canonical_relative_paths(candidate, allowed):
    scope = FileScope(writable=["src"])
    assert scope.is_writable(candidate) is allowed


def test_scope_guard_canonicalizes_known_workspace_paths(tmp_path):
    """运行时守卫负责把本地绝对路径归一后，再交给严格 FileScope。"""
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(writable=["src/a.py"]))
    target = tmp_path / "src" / "a.py"

    assert require_writable(str(target)) == ""


@pytest.mark.parametrize("path", [" src/a.py ", r"src\a.py", "src/../src/a.py", "src/./a.py"])
def test_write_file_uses_one_unambiguous_path_for_auth_and_io(tmp_path, path):
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(writable=["src/a.py"]))

    result = write_file.invoke({"path": path, "content": "owned\n"})

    assert not result.startswith("✅")
    assert not (tmp_path / "src" / "a.py").exists()
    assert not (tmp_path / " src" / "a.py ").exists()
    assert not (tmp_path / r"src\a.py").exists()


def test_allow_any_still_rejects_path_outside_workspace(tmp_path):
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(allow_any=True))
    outside = tmp_path.parent / "outside.txt"

    result = write_file.invoke({"path": str(outside), "content": "owned\n"})

    assert not result.startswith("✅")
    assert not outside.exists()


def test_scope_grant_and_request_share_symlink_canonicalization(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(writable=["alias/a.py"]))

    result = write_file.invoke({"path": "alias/a.py", "content": "ok\n"})

    assert result.startswith("✅")
    assert (real / "a.py").read_text() == "ok\n"

    diff = "--- a/real/a.py\n+++ b/real/a.py\n@@ -1 +1 @@\n-old\n+ok\n"
    assert _scope_violations(diff, FileScope(writable=["alias/a.py"])) == []
    assert _build_error_is_upstream(
        "real/a.py:1: error", "python", scope=FileScope(writable=["alias/a.py"])
    ) is False


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ((127, "", "ruff: not found"), "RUFF_TOOL_ERROR"),
        ((1, "not-json", ""), "RUFF_OUTPUT_INVALID"),
    ],
)
def test_python_lint_never_reports_broken_ruff_as_ok(monkeypatch, result, code):
    monkeypatch.setattr("swarm.worker.l1_pipeline._sandbox_ctx", lambda: object())
    monkeypatch.setattr("swarm.worker.l1_pipeline._run_check_split", lambda *_a, **_k: result)

    has_error, _messages, issues = _lint_python("/workspace", ["a.py"])

    assert has_error is True
    assert issues[0]["code"] == code


def test_python_lint_timeout_is_machine_readable_failure(monkeypatch):
    monkeypatch.setattr("swarm.worker.l1_pipeline._sandbox_ctx", lambda: object())

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ruff", 60)

    monkeypatch.setattr("swarm.worker.l1_pipeline._run_check_split", _timeout)
    has_error, _messages, issues = _lint_python("/workspace", ["a.py"])
    assert has_error is True
    assert issues[0]["code"] == "RUFF_TIMEOUT"


def test_delete_file_is_scope_aware_in_local_mode(tmp_path):
    allowed = tmp_path / "old.py"
    denied = tmp_path / "keep.py"
    allowed.write_text("old\n")
    denied.write_text("keep\n")
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(delete_files=["old.py"]))

    assert hasattr(file_tools, "delete_file")
    ok = file_tools.delete_file.invoke({"path": "old.py"})
    no = file_tools.delete_file.invoke({"path": "keep.py"})

    assert ok.startswith("✅")
    assert not allowed.exists()
    assert no.startswith("⛔")
    assert denied.exists()


def test_delete_file_unlinks_declared_symlink_not_its_target(tmp_path):
    real = tmp_path / "real.py"
    alias = tmp_path / "alias.py"
    real.write_text("keep\n")
    alias.symlink_to(real)
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(delete_files=["alias.py"]))

    result = file_tools.delete_file.invoke({"path": "alias.py"})

    assert result.startswith("✅")
    assert not alias.exists() and not alias.is_symlink()
    assert real.read_text() == "keep\n"


def test_delete_file_requires_exact_declared_leaf(tmp_path):
    target = tmp_path / "dir" / "keep.py"
    target.parent.mkdir()
    target.write_text("keep\n")
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(delete_files=["dir"]))

    result = file_tools.delete_file.invoke({"path": "dir/keep.py"})

    assert not result.startswith("✅")
    assert target.read_text() == "keep\n"


def test_delete_file_can_unlink_symlink_whose_target_is_directory(tmp_path):
    target_dir = tmp_path / "real-dir"
    target_dir.mkdir()
    alias = tmp_path / "dir-link"
    alias.symlink_to(target_dir, target_is_directory=True)
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(delete_files=["dir-link"]))

    result = file_tools.delete_file.invoke({"path": "dir-link"})

    assert result.startswith("✅")
    assert not alias.is_symlink()
    assert target_dir.is_dir()


def test_delete_file_uses_remote_sandbox_when_active(monkeypatch, tmp_path):
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(delete_files=["old.py"]))
    sandbox = SimpleNamespace(sandbox_id="sb-1")
    manager = MagicMock()
    manager.run_command.return_value = SimpleNamespace(
        success=True, error="", stdout="", stderr=""
    )
    set_sandbox_context(sandbox, manager)
    monkeypatch.setattr(
        "swarm.config.settings.get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(
                sandbox_first=True, sandbox_remote_workdir="/workspace"
            )
        ),
    )

    result = file_tools.delete_file.invoke({"path": "old.py"})

    assert result == "✅ 已删除沙箱文件 /workspace/old.py"
    command = manager.run_command.call_args.args[1]
    assert command.endswith("rm -- /workspace/old.py")


@pytest.mark.parametrize("path", ["evil/src/a.py", "src/../secret.py"])
def test_write_file_rejects_scope_aliases_before_disk_write(tmp_path, path):
    set_workspace_root(str(tmp_path))
    set_scope(FileScope(writable=["src/a.py", "src"]))

    result = write_file.invoke({"path": path, "content": "owned\n"})

    assert result.startswith("⛔")
    assert not (tmp_path / "evil" / "src" / "a.py").exists()
    assert not (tmp_path / "secret.py").exists()


def test_l1_rejects_prefixed_scope_alias():
    diff = (
        "--- a/evil/src/a.py\n"
        "+++ b/evil/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert files_from_unified_diff(diff) == ["evil/src/a.py"]
    assert _scope_violations(diff, FileScope(writable=["src/a.py"])) == [
        "evil/src/a.py"
    ]
