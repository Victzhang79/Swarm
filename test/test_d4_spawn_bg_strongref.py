"""D4：后台 fire-and-forget 任务经 _spawn_bg 强引用集，防 GC 静默回收。

行为测试 _spawn_bg 机制（project/config 的预处理/探测后台任务改走它）：运行中被强引用、
完成后回调清理、任务真跑完（不被回收）。swarm.api.app 名被 FastAPI 实例遮蔽 → importlib 取模块。
"""
from __future__ import annotations

import asyncio
import importlib

appmod = importlib.import_module("swarm.api.app")


async def test_spawn_bg_holds_ref_runs_and_cleans_up():
    done: list[int] = []

    async def _work():
        await asyncio.sleep(0.01)
        done.append(1)

    t = appmod._spawn_bg(_work())
    assert t in appmod._APP_BG_TASKS, "运行中应被强引用集持有（防 GC）"
    await t
    assert done == [1], "任务应真正跑完"
    assert t not in appmod._APP_BG_TASKS, "完成后 done_callback 应从集合移除"


async def test_spawn_bg_returns_task():
    async def _noop():
        return 42

    t = appmod._spawn_bg(_noop())
    assert isinstance(t, asyncio.Task)
    assert await t == 42
