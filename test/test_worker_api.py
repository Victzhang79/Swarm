#!/usr/bin/env python3
"""Worker API + apply-diff 单元测试（mock 端点 + diff_apply 真实 git）"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_files_from_unified_diff():
    from swarm.project.diff_apply import files_from_unified_diff

    diff = """--- a/foo.py
+++ b/foo.py
@@ -1 +1 @@
-old
+new
--- a/bar.py
+++ b/bar.py
"""
    paths = files_from_unified_diff(diff)
    assert paths == ["foo.py", "bar.py"]
    print("  ✅ files_from_unified_diff")


def test_apply_git_diff_in_repo():
    from swarm.project.diff_apply import apply_git_diff

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        init = subprocess.run(["git", "init", "-b", "main"], cwd=root, capture_output=True, text=True)
        if init.returncode != 0:
            init = subprocess.run(["git", "init"], cwd=root, capture_output=True, text=True)
        if init.returncode != 0:
            print(f"  ⚠ apply_git_diff 跳过（git init 不可用: {init.stderr[:120]}）")
            return
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
        f = root / "hello.txt"
        f.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "hello.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True)

        patch = """--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+hello world
"""
        check = apply_git_diff(str(root), patch, check_only=True)
        assert check["ok"], check
        applied = apply_git_diff(str(root), patch, check_only=False)
        assert applied["ok"], applied
        assert "hello world" in f.read_text(encoding="utf-8")
    print("  ✅ apply_git_diff (real git repo)")


def test_worker_run_endpoint():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    mock_project = {"id": "proj-1", "path": "/tmp/proj", "name": "test"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = mock_project
        with patch(
            "swarm.worker.runner.start_standalone_worker_background",
        ) as mock_start:
            client = TestClient(app)
            resp = client.post(
                "/api/projects/proj-1/worker/run",
                json={"description": "fix typo", "difficulty": "trivial"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "ok"
            assert body["project_id"] == "proj-1"
            assert body["run_id"]
            mock_start.assert_called_once()
    print("  ✅ POST /worker/run")


def test_worker_run_scope_payload():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    mock_project = {"id": "proj-1", "path": "/tmp/proj", "name": "test"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = mock_project
        with patch(
            "swarm.worker.runner.start_standalone_worker_background",
        ) as mock_start:
            client = TestClient(app)
            resp = client.post(
                "/api/projects/proj-1/worker/run",
                json={
                    "description": "fix",
                    "writable": ["src/a.py"],
                    "readable": ["src/", "tests/"],
                },
            )
            assert resp.status_code == 200, resp.text
            args, kwargs = mock_start.call_args
            assert kwargs.get("writable") == ["src/a.py"]
            assert kwargs.get("readable") == ["src/", "tests/"]
    print("  ✅ POST /worker/run scope payload")


def test_parse_scope_csv():
    from swarm.cli import _parse_scope_csv

    assert _parse_scope_csv("") is None
    assert _parse_scope_csv("  ") is None
    assert _parse_scope_csv("a.py,b.py") == ["a.py", "b.py"]
    assert _parse_scope_csv("a.py\nb.py") == ["a.py", "b.py"]
    print("  ✅ _parse_scope_csv")


def test_worker_run_404():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = None
        client = TestClient(app)
        resp = client.post(
            "/api/projects/missing/worker/run",
            json={"description": "x"},
        )
        assert resp.status_code == 404
    print("  ✅ POST /worker/run 404")


def test_apply_diff_endpoint():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {
        "id": "task-1",
        "project_id": "proj-1",
        "merged_diff": "--- a/x\n+++ b/x\n",
    }
    project = {"id": "proj-1", "path": "/tmp/p"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        mock_store.get_project.return_value = project
        with patch(
            "swarm.project.diff_apply.apply_git_diff",
            return_value={"ok": True, "stage": "check", "message": "git apply --check 通过"},
        ) as mock_apply:
            client = TestClient(app)
            resp = client.post(
                "/api/tasks/task-1/apply-diff",
                json={"check_only": True},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["ok"] is True
            mock_apply.assert_called_once()
            assert mock_apply.call_args.kwargs.get("check_only") is True
    print("  ✅ POST /apply-diff")


def test_apply_diff_no_merged_diff():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = {"id": "t", "project_id": "p", "merged_diff": ""}
        mock_store.get_project.return_value = {"path": "/tmp"}
        client = TestClient(app)
        resp = client.post("/api/tasks/t/apply-diff", json={})
        assert resp.status_code == 400
    print("  ✅ POST /apply-diff empty diff")


def test_project_apply_diff():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    project = {"id": "proj-1", "path": "/tmp/p"}
    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_project.return_value = project
        with patch(
            "swarm.project.diff_apply.apply_git_diff",
            return_value={"ok": True, "message": "applied"},
        ) as mock_apply:
            client = TestClient(app)
            resp = client.post(
                "/api/projects/proj-1/apply-diff",
                json={"diff": "--- a/x\n+++ b/x\n", "check_only": False},
            )
            assert resp.status_code == 200, resp.text
            mock_apply.assert_called_once()
    print("  ✅ POST /projects/{id}/apply-diff")


def test_approve_with_apply_diff():
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {
        "id": "task-1",
        "project_id": "proj-1",
        "merged_diff": "--- a/x\n+++ b/x\n",
        "status": "DELIVERING",
    }
    project = {"id": "proj-1", "path": "/tmp/p"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        mock_store.get_project.return_value = project
        mock_store.update_task.return_value = task
        with patch("swarm.project.diff_apply.apply_git_diff", return_value={"ok": True}):
            with patch("swarm.brain.runner.resume_task_background"):
                with patch("swarm.brain.runner.register_task_queue"):
                    client = TestClient(app)
                    resp = client.post(
                        "/api/tasks/task-1/approve",
                        json={"apply_diff": True},
                    )
                    assert resp.status_code == 200, resp.text
                    body = resp.json()
                    assert body.get("apply_diff", {}).get("ok") is True
    print("  ✅ POST /approve apply_diff")


def main() -> int:
    tests = [
        test_files_from_unified_diff,
        test_apply_git_diff_in_repo,
        test_worker_run_endpoint,
        test_worker_run_scope_payload,
        test_parse_scope_csv,
        test_worker_run_404,
        test_apply_diff_endpoint,
        test_apply_diff_no_merged_diff,
        test_project_apply_diff,
        test_approve_with_apply_diff,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        return 1
    print(f"\n✅ 全部 {len(tests)} 项 worker/apply-diff 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
