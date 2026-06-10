#!/usr/bin/env python3
"""取消任务释放沙箱 + 日志实时流（TaskLogPoller）单测。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── SandboxManager.kill_by_task ──────────────────


def test_sandboxes_for_task_filters_by_task_id():
    from swarm.worker.sandbox import SandboxManager

    mgr = SandboxManager.__new__(SandboxManager)
    mgr._sandbox_meta = {
        "sb-1": {"project_id": "p1", "task_id": "task-A", "source": "worker"},
        "sb-2": {"project_id": "p1", "task_id": "task-A", "source": "worker"},
        "sb-3": {"project_id": "p1", "task_id": "task-B", "source": "worker"},
    }
    assert mgr.sandboxes_for_task("task-A") == {"sb-1", "sb-2"}
    assert mgr.sandboxes_for_task("task-B") == {"sb-3"}
    assert mgr.sandboxes_for_task("task-X") == set()
    print("  ✅ sandboxes_for_task 按 task_id 过滤")


def test_kill_by_task_kills_matching_sandboxes():
    from swarm.worker.sandbox import SandboxManager

    mgr = SandboxManager.__new__(SandboxManager)
    mgr._sandbox_meta = {
        "sb-1": {"project_id": "p1", "task_id": "task-A", "source": "worker"},
        "sb-2": {"project_id": "p1", "task_id": "task-A", "source": "worker"},
        "sb-3": {"project_id": "p1", "task_id": "task-B", "source": "worker"},
    }
    killed_ids = []
    mgr.kill = MagicMock(side_effect=lambda sid: killed_ids.append(sid))

    n = mgr.kill_by_task("task-A")
    assert n == 2
    assert set(killed_ids) == {"sb-1", "sb-2"}
    # task-B 不受影响
    assert "sb-3" not in killed_ids
    print("  ✅ kill_by_task 只 kill 该任务的沙箱")


def test_kill_by_task_no_match_returns_zero():
    from swarm.worker.sandbox import SandboxManager

    mgr = SandboxManager.__new__(SandboxManager)
    mgr._sandbox_meta = {}
    mgr.kill = MagicMock()
    assert mgr.kill_by_task("task-X") == 0
    mgr.kill.assert_not_called()
    print("  ✅ kill_by_task 无匹配返回 0 不调用 kill")


# ── cancel_task 释放沙箱 ─────────────────────────


def test_cancel_task_releases_sandboxes():
    """取消任务时应调用 kill_by_task 释放容器资源（核心修复）。"""
    import asyncio

    from swarm.brain import runner

    fake_mgr = MagicMock()
    fake_mgr.kill_by_task.return_value = 2

    with patch.object(runner.store, "get_task", return_value={"id": "task-A", "status": "DISPATCHING"}):
        with patch.object(runner.store, "update_task"):
            with patch("swarm.worker.sandbox.get_sandbox_manager", return_value=fake_mgr):
                # 无 handle 也应执行沙箱清理
                runner._task_handles.pop("task-A", None)
                asyncio.run(runner.cancel_task("task-A"))

    fake_mgr.kill_by_task.assert_called_once_with("task-A")
    print("  ✅ cancel_task 调用 kill_by_task 释放沙箱")


# ── TaskLogPoller ────────────────────────────────


def test_log_poller_prime_then_incremental(tmp_path):
    from swarm.config.settings import reload_config

    log = tmp_path / "swarm.log"
    log.write_text(
        "2026 [INFO] swarm.x [task=abcd1234]: line 1\n"
        "2026 [INFO] swarm.y: 无关行\n",
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"SWARM_LOG_FILE": str(log)}):
        reload_config()
        from swarm.logging_config import TaskLogPoller

        poller = TaskLogPoller("abcd1234-full-uuid")
        first = poller.poll()  # prime：回放已有匹配
        assert any("line 1" in ln for ln in first)
        assert not any("无关行" in ln for ln in first)

        # 追加新行后再 poll，只拿增量
        with open(log, "a", encoding="utf-8") as f:
            f.write("2026 [INFO] swarm.z [task=abcd1234]: line 2\n")
            f.write("2026 [INFO] swarm.w [task=99999999]: 别的任务\n")
        second = poller.poll()
        assert any("line 2" in ln for ln in second)
        assert not any("别的任务" in ln for ln in second)
        assert not any("line 1" in ln for ln in second)  # 不重复回放

        # 无新增 → 空
        assert poller.poll() == []
    reload_config()
    print("  ✅ TaskLogPoller prime + 增量 + 过滤其他任务 + 无重复")


def test_log_poller_no_file_graceful(tmp_path):
    from swarm.config.settings import reload_config

    missing = tmp_path / "nope.log"
    with patch.dict("os.environ", {"SWARM_LOG_FILE": str(missing)}):
        reload_config()
        from swarm.logging_config import TaskLogPoller

        poller = TaskLogPoller("abcd1234")
        assert poller.poll() == []
    reload_config()
    print("  ✅ TaskLogPoller 文件不存在优雅返回空")


def test_log_stream_endpoint_404():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = None
        client = TestClient(app)
        resp = client.get("/api/tasks/ghost/logs/stream")
        assert resp.status_code == 404
    print("  ✅ GET /logs/stream 任务不存在 404")


# ── 批量清理端点 ─────────────────────────────────


def test_cleanup_endpoint_all():
    """POST /api/sandbox/cleanup 批量销毁追踪中的沙箱。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    fake_mgr = MagicMock()
    fake_mgr._instances = {"sb-1": object(), "sb-2": object()}
    fake_mgr.kill = MagicMock()

    with patch("swarm.api.app._get_sandbox_manager", return_value=fake_mgr):
        client = TestClient(app)
        resp = client.post("/api/sandbox/cleanup")
        assert resp.status_code == 200, resp.text
        assert resp.json()["killed"] == 2
        assert fake_mgr.kill.call_count == 2
    print("  ✅ /api/sandbox/cleanup 批量销毁追踪沙箱")


def test_cleanup_endpoint_by_task():
    """cleanup?task_id=X 只销毁该任务的沙箱。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    fake_mgr = MagicMock()
    fake_mgr.kill_by_task.return_value = 3

    with patch("swarm.api.app._get_sandbox_manager", return_value=fake_mgr):
        client = TestClient(app)
        resp = client.post("/api/sandbox/cleanup?task_id=task-X")
        assert resp.status_code == 200, resp.text
        assert resp.json()["scope"] == "task"
        assert resp.json()["killed"] == 3
        fake_mgr.kill_by_task.assert_called_once_with("task-X")
    print("  ✅ /api/sandbox/cleanup?task_id 按任务销毁")


def test_orphans_endpoint_identifies_unassociated():
    """GET /api/sandbox/orphans 识别无项目/任务关联的沙箱。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    server_list = [
        {"id": "sb-assoc", "status": "running"},     # 有项目关联 → 非孤儿
        {"id": "sb-orphan1", "status": "running"},    # meta 缺失 → 孤儿
        {"id": "sb-orphan2", "status": "running"},    # meta 有但 project/task 皆空 → 孤儿
    ]
    fake_mgr = MagicMock()
    meta_map = {
        "sb-assoc": {"project_id": "p1", "task_id": "t1"},
        "sb-orphan1": None,
        "sb-orphan2": {"project_id": "", "task_id": ""},
    }
    fake_mgr.get_sandbox_meta = lambda sid: meta_map.get(sid)

    with patch("swarm.api.app._get_sandbox_manager", return_value=fake_mgr):
        with patch("swarm.api.app._fetch_sandbox_list_from_server", return_value=server_list):
            client = TestClient(app)
            resp = client.get("/api/sandbox/orphans")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total"] == 3
            assert data["orphan_count"] == 2
            ids = {o["id"] for o in data["orphans"]}
            assert ids == {"sb-orphan1", "sb-orphan2"}
            assert "sb-assoc" not in ids
    print("  ✅ /api/sandbox/orphans 正确识别孤儿（无项目/任务关联）")


def test_cleanup_orphans_only():
    """cleanup?orphans_only=true 只销毁孤儿，不误伤有关联的沙箱。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    server_list = [
        {"id": "sb-assoc"},
        {"id": "sb-orphan1"},
    ]
    fake_mgr = MagicMock()
    meta_map = {"sb-assoc": {"project_id": "p1", "task_id": "t1"}, "sb-orphan1": None}
    fake_mgr.get_sandbox_meta = lambda sid: meta_map.get(sid)
    killed = []
    fake_mgr.kill = MagicMock(side_effect=lambda sid: killed.append(sid))

    with patch("swarm.api.app._get_sandbox_manager", return_value=fake_mgr):
        with patch("swarm.api.app._fetch_sandbox_list_from_server", return_value=server_list):
            client = TestClient(app)
            resp = client.post("/api/sandbox/cleanup?orphans_only=true")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["scope"] == "orphans"
            assert data["killed"] == 1
            assert killed == ["sb-orphan1"], "只 kill 孤儿，不动 sb-assoc"
    print("  ✅ /api/sandbox/cleanup?orphans_only 只清孤儿不误伤")


if __name__ == "__main__":
    import inspect
    import tempfile

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = inspect.signature(fn).parameters
            if "tmp_path" in params:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            elif params:
                continue
            else:
                fn()
    print("\n取消释放沙箱/日志流 单测通过。")
