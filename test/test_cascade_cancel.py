#!/usr/bin/env python3
"""删项目/取消任务级联终止 回归测试。

复现并防止幽灵任务 bug：删除项目时若不取消运行中任务，asyncio 句柄会因 DB
记录被删而失去取消入口（旧 cancel_task: `if not task: return False`），陷入
replan 死循环持续烧 GPU。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_cancel_task_handles_missing_db_record():
    """DB 记录缺失(项目已删)时，cancel_task 仍取消内存句柄并返回 True。"""
    from swarm.brain import runner

    async def _run():
        async def _forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                raise

        handle = asyncio.create_task(_forever())
        runner._task_handles["ghost-1"] = handle
        await asyncio.sleep(0.05)

        # 模拟 DB 记录已删 + 沙箱管理器无操作
        with patch.object(runner.store, "get_task", return_value=None), \
             patch("swarm.worker.sandbox.get_sandbox_manager") as gsm:
            gsm.return_value.kill_by_task.return_value = 0
            result = await runner.cancel_task("ghost-1")

        assert result is True, "DB 记录缺失但有活跃句柄时应返回 True"
        assert handle.cancelled() or handle.done(), "幽灵任务句柄应被取消"
        runner._task_handles.pop("ghost-1", None)
        print("  ✅ cancel_task 能终止无 DB 记录的幽灵任务")

    asyncio.run(_run())


def test_cancel_task_truly_missing_returns_false():
    """既无 DB 记录又无内存句柄 → 返回 False（无事可做）。"""
    from swarm.brain import runner

    async def _run():
        with patch.object(runner.store, "get_task", return_value=None), \
             patch("swarm.worker.sandbox.get_sandbox_manager") as gsm:
            gsm.return_value.kill_by_task.return_value = 0
            result = await runner.cancel_task("nonexistent-xyz")
        assert result is False
        print("  ✅ 完全不存在的任务返回 False")

    asyncio.run(_run())


def test_cancel_project_tasks_cancels_active_handles():
    """cancel_project_tasks 取消该项目所有活跃任务。"""
    from swarm.brain import runner

    async def _run():
        async def _forever():
            await asyncio.sleep(3600)

        h1 = asyncio.create_task(_forever())
        h2 = asyncio.create_task(_forever())
        runner._task_handles["proj-task-1"] = h1
        runner._task_handles["proj-task-2"] = h2
        await asyncio.sleep(0.05)

        def fake_get_task(tid):
            return {"id": tid, "project_id": "proj-X", "status": "DISPATCHING"}

        with patch.object(runner.store, "get_task", side_effect=fake_get_task), \
             patch.object(runner.store, "list_tasks", return_value=[]), \
             patch("swarm.worker.sandbox.get_sandbox_manager") as gsm:
            gsm.return_value.kill_by_task.return_value = 0
            n = await runner.cancel_project_tasks("proj-X")

        assert n == 2, f"应取消 2 个任务，实际 {n}"
        assert (h1.cancelled() or h1.done()) and (h2.cancelled() or h2.done())
        runner._task_handles.pop("proj-task-1", None)
        runner._task_handles.pop("proj-task-2", None)
        print("  ✅ cancel_project_tasks 级联取消项目内活跃任务")

    asyncio.run(_run())


def test_cancel_project_tasks_skips_other_projects():
    """只取消目标项目的任务，不误伤其他项目。"""
    from swarm.brain import runner

    async def _run():
        async def _forever():
            await asyncio.sleep(3600)

        h_other = asyncio.create_task(_forever())
        runner._task_handles["other-proj-task"] = h_other
        await asyncio.sleep(0.05)

        def fake_get_task(tid):
            return {"id": tid, "project_id": "proj-OTHER", "status": "DISPATCHING"}

        with patch.object(runner.store, "get_task", side_effect=fake_get_task), \
             patch.object(runner.store, "list_tasks", return_value=[]), \
             patch("swarm.worker.sandbox.get_sandbox_manager") as gsm:
            gsm.return_value.kill_by_task.return_value = 0
            n = await runner.cancel_project_tasks("proj-TARGET")

        assert n == 0, "不应取消其他项目的任务"
        assert not h_other.cancelled(), "其他项目任务不应被取消"
        h_other.cancel()
        runner._task_handles.pop("other-proj-task", None)
        print("  ✅ cancel_project_tasks 不误伤其他项目")

    asyncio.run(_run())


if __name__ == "__main__":
    test_cancel_task_handles_missing_db_record()
    test_cancel_task_truly_missing_returns_false()
    test_cancel_project_tasks_cancels_active_handles()
    test_cancel_project_tasks_skips_other_projects()
    print("\n级联取消 回归测试通过。")
