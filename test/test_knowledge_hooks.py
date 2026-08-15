#!/usr/bin/env python3
"""approve → schedule_incremental_update / KnowledgeUpdater 钩子测试"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_approve_no_longer_triggers_kb_from_endpoint():
    """★对抗复核 3rd#1 治本★：approve 端点【不再】在 apply 前读磁盘触发 KB 索引（会用 L2 回滚后
    的旧内容覆盖知识库）。KB 索引已移到 learn_success commit 之后，见下方 test。"""
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {
        "id": "task-1", "project_id": "proj-1",
        "merged_diff": "--- a/foo.py\n+++ b/foo.py\n", "status": "DELIVERING",
    }
    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        mock_store.get_project.return_value = {"id": "proj-1", "path": "/tmp/p"}
        mock_store.update_task.return_value = task
        mock_store.claim_human_gate.return_value = task
        with patch("swarm.brain.runner.resume_task_background"):
            with patch("swarm.brain.runner.register_task_queue"):
                with patch("swarm.knowledge.hooks.schedule_incremental_update") as mock_hook:
                    client = TestClient(app)
                    resp = client.post("/api/tasks/task-1/approve", json={})
                    assert resp.status_code == 200, resp.text
                    mock_hook.assert_not_called()  # 端点不再触发
    print("  ✅ approve 端点不再触发 KB（移到 learn_success 后）")


def test_learn_success_triggers_kb_after_commit():
    """批25 GS-5w 换锁：原命题「KB 索引由 learn_success 在 commit 成功后触发（3rd#1）——
    读到的是 apply 后的正确产出」。

    行为锁：真跑 learn_success，按调用顺序记录「交付 commit」与「schedule_incremental_update」
    两个真实调用点——KB 触发必须严格在 commit 之后。把 KB 触发移出 commit 成功分支（或挪到
    apply 前）即红。"""
    import asyncio

    from swarm.brain import nodes
    from swarm.types import Complexity

    order: list[str] = []

    async def _fake_deliver(proj_path, merged_diff, base_commit, out_files, task_id):
        order.append("deliver_commit")  # reset→apply→commit 全在此收口点内完成
        return {
            "ap": {"ok": True, "applied": ["a.py"], "failed": []},
            "out_files": ["a.py"], "wm": {},
            "commit": {"ok": True, "committed": True, "commit_hash": "abc123"},
        }

    async def _fake_persist(state, parsed):
        return {"persisted": False}

    with patch("swarm.brain.nodes._deliver_merged_diff_serialized", _fake_deliver), \
         patch("swarm.brain.nodes._get_project_path", lambda pid: "/tmp/fake-proj"), \
         patch("swarm.knowledge.hooks.schedule_incremental_update",
               lambda *a, **k: order.append("kb_index")), \
         patch("swarm.brain.learn_store.persist_learn_success", _fake_persist):
        out = asyncio.run(nodes.learn_success({
            "task_id": "t1", "project_id": "p1", "task_description": "d",
            "merged_diff": "--- a/a.py\n+++ b/a.py\n@@ +x\n",
            "complexity": Complexity.SIMPLE,  # SIMPLE 路径不走 LLM，聚焦交付→KB 触发面
        }))
    assert out.get("learned") is True, "前提：learn_success 必须真走完"
    assert order == ["deliver_commit", "kb_index"], (
        "KB 增量索引必须在交付 commit 之后触发（否则读到 apply 前旧内容，3rd#1 回归）: "
        f"实际顺序={order}")


def test_build_changes_emits_deleted_for_absent_files(tmp_path):
    """3rd#1：diff 里有但磁盘已不在的文件 → DELETED（清旧向量），不再静默跳过。"""
    from swarm.knowledge.hooks import _build_changes
    from swarm.knowledge.updater import ChangeType

    # gone.py 在 diff 里但不落盘 → DELETED；kept.py 落盘 → MODIFIED
    (tmp_path / "kept.py").write_text("x = 1\n", encoding="utf-8")
    diff = (
        "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
        "--- a/kept.py\n+++ b/kept.py\n@@ -1 +1 @@\n-x=0\n+x = 1\n"
    )
    changes = {c.file_path: c.change_type for c in _build_changes(str(tmp_path), diff)}
    assert changes.get("gone.py") == ChangeType.DELETED
    assert changes.get("kept.py") == ChangeType.MODIFIED


def test_approve_idempotent_when_claim_lost():
    """P1-A：认领失败（None，双击第二次/非审核态）→ 200 幂等，不 apply、不触发 resume、不发 hook。"""
    from fastapi.testclient import TestClient
    from swarm.api.app import app

    task = {"id": "t9", "project_id": "p9", "merged_diff": "--- a\n+++ b\n", "status": "MONITORING"}

    with patch("swarm.api.app.store") as mock_store:
        mock_store.get_task.return_value = task
        mock_store.get_project.return_value = {"id": "p9", "path": "/tmp/p"}
        mock_store.claim_human_gate.return_value = None  # 认领失败（已被处理/非审核态）
        with patch("swarm.brain.runner.resume_task_background") as mock_resume:
            with patch("swarm.brain.runner.register_task_queue"):
                with patch("swarm.knowledge.hooks.schedule_incremental_update") as mock_hook:
                    client = TestClient(app)
                    resp = client.post("/api/tasks/t9/approve", json={})
                    assert resp.status_code == 200
                    mock_hook.assert_not_called()
                    mock_resume.assert_not_called()
    print("  ✅ approve 认领失败 → 幂等无副作用")


def test_incremental_update_from_task_calls_updater():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from swarm.knowledge.hooks import incremental_update_from_task

    with patch("swarm.knowledge.hooks.enqueue_kb_update", new_callable=AsyncMock) as mock_eq:
        mock_eq.return_value = 99
        with patch("swarm.knowledge.hooks._build_changes", return_value=[object()]):
            result = asyncio.run(
                incremental_update_from_task("p1", "/tmp", "--- a/x\n+++ b/x\n", task_id="t1")
            )
    assert result["status"] == "queued"
    assert result["event_id"] == 99
    mock_eq.assert_awaited_once()
    print("  ✅ incremental_update_from_task → enqueue")


def main() -> int:
    tests = [
        test_approve_no_longer_triggers_kb_from_endpoint,
        test_learn_success_triggers_kb_after_commit,
        test_incremental_update_from_task_calls_updater,
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
    print(f"\n✅ 全部 {len(tests)} 项 knowledge hooks 测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
