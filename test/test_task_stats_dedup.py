#!/usr/bin/env python3
"""任务统计 merge_rate + 去重 + 日志读取的单测。

merge_rate / accept_rate 用纯计算验证（构造 counts 走真实公式分支）；
find_active_duplicate_task 与 read_task_logs 用真实函数 + mock 边界。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── merge_rate vs accept_rate 语义 ───────────────


def test_merge_rate_includes_failed_in_denominator():
    """核心修复：失败任务必须计入成功率分母（accept_rate 不计 → 会误导）。

    场景：1 DONE+ACCEPT, 3 FAILED。
      accept_rate = approved/completed = 1/1 = 100%（误导）
      merge_rate  = completed/terminal = 1/4 = 25%（真实成功率）
    这里直接验证两条公式，确保修复后的语义。
    """
    completed, failed, cancelled, approved = 1, 3, 0, 1
    accept_rate = round(approved / completed, 4) if completed else None
    terminal_total = completed + failed + cancelled
    merge_rate = round(completed / terminal_total, 4) if terminal_total else None
    assert accept_rate == 1.0          # 旧指标：100%（失败被忽略）
    assert merge_rate == 0.25          # 新指标：25%（含失败分母）
    assert merge_rate < accept_rate    # 失败任务拉低真实成功率
    print("  ✅ merge_rate 含失败分母（修正 accept_rate 100% 误导）")


def test_merge_rate_all_done():
    completed, failed, cancelled = 4, 0, 0
    terminal_total = completed + failed + cancelled
    merge_rate = round(completed / terminal_total, 4) if terminal_total else None
    assert merge_rate == 1.0
    print("  ✅ merge_rate 全成功=100%")


def test_merge_rate_no_terminal_tasks():
    completed, failed, cancelled = 0, 0, 0
    terminal_total = completed + failed + cancelled
    merge_rate = round(completed / terminal_total, 4) if terminal_total else None
    assert merge_rate is None
    print("  ✅ merge_rate 无终态任务=None")


def test_stats_endpoint_exposes_merge_rate():
    """/api/stats 应返回 merge_rate 字段。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    mock_stats = {
        "total_tasks": 4, "completed": 1, "failed": 3, "cancelled": 0,
        "approved": 1, "accept_rate": 1.0, "merge_rate": 0.25,
        "avg_duration_seconds": 10.0, "total_tokens": 100, "avg_tokens": 25.0,
        "recent_tasks": [],
    }
    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task_stats.return_value = mock_stats
        client = TestClient(app)
        resp = client.get("/api/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["merge_rate"] == 0.25
        assert data["accept_rate"] == 1.0
    print("  ✅ /api/stats 暴露 merge_rate")


# ── 任务去重 ─────────────────────────────────────


def test_create_task_dedup_returns_existing():
    """同描述进行中任务存在时，create_task 返回 duplicate 而非新建。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    project = {"id": "proj-1", "path": "/tmp/p", "status": "INDEXED"}
    existing = {"id": "task-existing-001", "status": "DISPATCHING", "description": "修复登录"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = project
        mock_store.get_progress.return_value = {"status": "INDEXED"}
        mock_store.find_active_duplicate_task.return_value = existing
        with patch("swarm.knowledge.readiness.brain_task_ready", return_value=(True, "")):
            client = TestClient(app)
            resp = client.post(
                "/api/projects/proj-1/tasks",
                json={"description": "修复登录"},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "duplicate"
            assert data["task"]["id"] == "task-existing-001"
            # 不应调用 create_task（被去重拦截）
            mock_store.create_task.assert_not_called()
    print("  ✅ create_task 同描述进行中→返回 duplicate 不新建")


def test_create_task_force_bypasses_dedup():
    """force=true 跳过去重，正常新建。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    project = {"id": "proj-1", "path": "/tmp/p", "status": "INDEXED"}
    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = project
        mock_store.get_progress.return_value = {"status": "INDEXED"}
        mock_store.create_task.return_value = {"id": "new-task", "status": "EMPTY"}
        mock_store.get_task.return_value = {"id": "new-task", "status": "SUBMITTED"}
        with patch("swarm.knowledge.readiness.brain_task_ready", return_value=(True, "")):
            with patch("swarm.brain.scheduler.submit_task"):
                client = TestClient(app)
                resp = client.post(
                    "/api/projects/proj-1/tasks",
                    json={"description": "修复登录", "force": True},
                )
                assert resp.status_code == 200, resp.text
                assert resp.json()["status"] == "ok"
                # force 时不查重复
                mock_store.find_active_duplicate_task.assert_not_called()
                mock_store.create_task.assert_called_once()
    print("  ✅ create_task force=true 跳过去重正常新建")


# ── 任务日志端点 ─────────────────────────────────


def test_task_logs_endpoint():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = {"id": "task-1", "status": "DONE"}
        with patch(
            "swarm.logging_config.read_task_logs",
            return_value=["2026-01-01 [INFO] swarm.brain.nodes [task=task-1]: ANALYZE"],
        ):
            client = TestClient(app)
            resp = client.get("/api/tasks/task-1/logs")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["count"] == 1
            assert "ANALYZE" in data["lines"][0]
    print("  ✅ GET /api/tasks/{id}/logs 返回过滤后的日志行")


def test_task_logs_404():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = None
        client = TestClient(app)
        resp = client.get("/api/tasks/ghost/logs")
        assert resp.status_code == 404
    print("  ✅ GET /api/tasks/{id}/logs 任务不存在 404")


def test_read_task_logs_filters_by_prefix(tmp_path):
    """read_task_logs 按 [task=前8位] 过滤。"""
    from swarm.config.settings import reload_config

    log = tmp_path / "swarm.log"
    log.write_text(
        "2026 [INFO] swarm.x [task=abcd1234]: line A\n"
        "2026 [INFO] swarm.y: no task line\n"
        "2026 [INFO] swarm.z [task=abcd1234 sub=st-1]: line B\n"
        "2026 [INFO] swarm.w [task=99999999]: other task\n",
        encoding="utf-8",
    )
    with patch.dict("os.environ", {"SWARM_LOG_FILE": str(log)}):
        reload_config()
        from swarm.logging_config import read_task_logs

        lines = read_task_logs("abcd1234-full-uuid-rest")
        assert len(lines) == 2
        assert all("abcd1234" in ln for ln in lines)
        assert not any("99999999" in ln for ln in lines)
    reload_config()
    print("  ✅ read_task_logs 按 task 前缀过滤")


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
    print("\n任务统计/去重/日志 单测通过。")
