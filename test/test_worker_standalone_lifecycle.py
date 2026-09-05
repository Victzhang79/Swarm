"""Standalone Worker scope、锁接线与 UI 终态回归。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm.api.routers.worker import WorkerRunRequest
from swarm.types import Confidence, WorkerOutput
from swarm.worker.runner import (
    _standalone_lock_keys,
    _standalone_scope,
    _standalone_worker_lock_can_narrow,
)


def test_standalone_api_documents_partial_scope_as_minimum_privilege():
    schema = WorkerRunRequest.model_json_schema()["properties"]
    assert "仅限" in schema["writable"]["description"]
    assert "仅限" in schema["readable"]["description"]


def test_standalone_explicit_empty_scope_does_not_expand_to_full_project():
    assert _standalone_scope(None, None).allow_any is True
    explicit_empty = _standalone_scope([], None)
    assert explicit_empty.allow_any is False
    assert explicit_empty.is_writable("secret.txt") is False


def test_standalone_partial_scope_can_authorize_exact_delete():
    scope = _standalone_scope(None, None, None, ["old.py"])
    assert scope.allow_any is False
    assert scope.delete_files == ["old.py"]
    assert scope.is_writable("old.py") is True
    assert _standalone_lock_keys(
        None, None, ["old.py"], remote_sandbox=False
    ) == ["default"]
    assert _standalone_lock_keys(
        None, None, ["module/old.py"], remote_sandbox=True
    ) == ["module"]
    fallback_cfg = SimpleNamespace(
        sandbox=SimpleNamespace(
            use_for_worker=True,
            api_url="https://sandbox",
            allow_local_fallback=True,
        )
    )
    assert _standalone_worker_lock_can_narrow(fallback_cfg) is False


def test_brain_keeps_project_wide_lock_for_explicit_local_worker(monkeypatch):
    from swarm.brain import runner

    monkeypatch.setattr(
        runner,
        "get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(use_for_worker=False, api_url="")
        ),
    )
    assert runner._brain_worker_lock_can_narrow() is False
    monkeypatch.setattr(
        runner,
        "get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(
                use_for_worker=True,
                api_url="https://sandbox",
                allow_local_fallback=False,
            )
        ),
    )
    assert runner._brain_worker_lock_can_narrow() is True
    monkeypatch.setattr(
        runner,
        "get_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(
                use_for_worker=True,
                api_url="https://sandbox",
                allow_local_fallback=True,
            )
        ),
    )
    assert runner._brain_worker_lock_can_narrow() is False


@pytest.mark.asyncio
async def test_brain_plan_event_does_not_upgrade_lock_when_local_fallback_possible(
    monkeypatch,
):
    """生产 plan 事件接线锁：可回落宿主写树时，即使 plan 有模块写集，
    也必须保留 default 整项目锁；删掉调用点 helper 裁决后此测试必红。"""
    from swarm.brain import runner

    plan = {"subtasks": [{"scope": {"writable": ["module/a.py"]}}]}

    class _Snapshot:
        values = {"plan": plan}
        interrupts = None

    class _Graph:
        async def astream_events(self, *_args, **_kwargs):
            yield {
                "event": "on_chain_end",
                "name": "plan",
                "data": {"output": {"plan": plan}},
            }

        async def aget_state(self, _config):
            return _Snapshot()

    class _Lock:
        key = "default"

        def renew(self):
            return True

    cfg = SimpleNamespace(
        sandbox=SimpleNamespace(
            use_for_worker=True,
            api_url="https://sandbox",
            allow_local_fallback=True,
        ),
        task_deadline_s=3600,
        task_deadline_per_subtask_s=0,
        max_task_tokens=0,
        max_task_tokens_per_subtask=0,
    )
    old_lock = _Lock()
    holder = {"lock": old_lock}
    monkeypatch.setattr(runner, "get_config", lambda: cfg)
    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _Graph())
    monkeypatch.setattr(
        runner.store,
        "get_task",
        lambda _tid: {"project_id": "p", "description": "d", "plan": None},
    )
    monkeypatch.setattr(runner.store, "update_task", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner.store, "check_task_token_limit", lambda *_a, **_k: (True, {})
    )
    monkeypatch.setattr("swarm.models.ledger.attach", lambda *_a, **_k: None)
    monkeypatch.setattr("swarm.models.ledger.widen_budget", lambda *_a, **_k: None)
    upgrade_calls = []
    monkeypatch.setattr(
        "swarm.infra.redis_client.upgrade_module_lock",
        lambda *_a, **_k: upgrade_calls.append(True),
    )

    await runner._stream_brain_events(
        "task-lock-wire", {"description": "d"}, runner._FanoutTopic(),
        project_id="p", lock_holder=holder,
    )

    assert upgrade_calls == []
    assert holder["lock"] is old_lock


@pytest.mark.asyncio
async def test_standalone_fallback_uses_real_default_lock_callsite(tmp_path, monkeypatch):
    """standalone 真实入口必须把 fallback 裁决传给锁类选择。"""
    from swarm.infra import redis_client
    from swarm.worker import runner
    from swarm.worker import executor as executor_module

    created = []

    class _DefaultLock:
        def __init__(self, project_id, module_key):
            created.append(("default", project_id, module_key))

        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            created.append(("release",))

    class _ModuleLock:
        def __init__(self, *_args, **_kwargs):
            created.append(("module",))

    class _Executor:
        def __init__(self, *_args, **_kwargs):
            self.phase = SimpleNamespace(value="PREPARING")
            self.execution_log = []

        async def run(self):
            return WorkerOutput(
                subtask_id="run-fallback-wire",
                diff="",
                summary="done",
                confidence=Confidence.HIGH,
                l1_passed=True,
            )

    cfg = SimpleNamespace(
        sandbox=SimpleNamespace(
            use_for_worker=True,
            api_url="https://sandbox",
            allow_local_fallback=True,
        )
    )
    monkeypatch.setattr("swarm.config.settings.get_config", lambda: cfg)
    monkeypatch.setattr(
        runner.store,
        "get_project",
        lambda _pid: {"id": "p", "path": str(tmp_path), "status": "READY"},
    )
    monkeypatch.setattr(redis_client, "ModuleLock", _DefaultLock)
    monkeypatch.setattr(redis_client, "MultiModuleLock", _ModuleLock)
    monkeypatch.setattr(executor_module, "WorkerExecutor", _Executor)
    runner.register_worker_queue("run-fallback-wire")
    try:
        await runner.run_standalone_worker(
            "run-fallback-wire", "p", "x", writable=["module/a.py"]
        )
    finally:
        runner._worker_running.discard("run-fallback-wire")
        runner._worker_queues.pop("run-fallback-wire", None)

    assert ("default", "p", "default") in created
    assert ("module",) not in created

def test_worker_ui_renders_failed_complete_event_as_failure():
    if not shutil.which("node"):
        return
    source = Path("api/static/js/tabs/worker.js").read_text()
    script = (
        "const vm=require('vm'); const src=" + repr(source) + ";"
        "const box={}; vm.createContext(box); vm.runInContext(src, box);"
        "const failed=box.workerEventState({step:'complete',status:'failed'},'progress');"
        "const done=box.workerEventState({step:'complete',status:'done'},'progress');"
        "if(!failed.failed||failed.logLevel!=='error'||failed.terminalLabel!=='失败')process.exit(2);"
        "if(done.failed||done.logLevel!=='info'||done.terminalLabel!=='完成')process.exit(3);"
    )
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
