#!/usr/bin/env python3
"""知识检索跨事件循环复用回归测试。

复现并防止 "asyncio.Lock is bound to a different event loop" bug：
retriever 单例持有的 psycopg.AsyncConnection 绑定到创建它的 loop，原
retrieve_knowledge_sync 用 asyncio.run 每次新建临时 loop，导致第二次调用
（如任务重试 dispatch）复用已死 loop 的连接而永久卡住。

修复：所有 retriever 协程路由到专用持久 KB loop。本测试用 mock retriever
验证多次 sync 调用 + 跨 loop async 调用都打到同一个 KB loop，不报错。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _make_fake_retriever():
    """伪 retriever：retrieve_for_brain 记录它运行在哪个 loop 上。"""
    fake = MagicMock()
    seen_loops = []

    async def _retrieve(task_desc, project_id, extra_keywords=None):
        seen_loops.append(id(asyncio.get_running_loop()))
        result = MagicMock()
        result.context = {"struct": [], "semantic": [], "norms": [], "behavior": [],
                          "mistakes": [], "successes": []}
        result.stats = {"struct_count": 0}
        return result

    fake.retrieve_for_brain = _retrieve
    fake._semantic = None
    return fake, seen_loops


def test_sync_calls_share_one_persistent_loop():
    """多次 retrieve_knowledge_sync 应跑在【同一个】持久 KB loop 上（不再新建临时 loop）。"""
    from swarm.knowledge import service

    fake, seen = _make_fake_retriever()
    # 重置单例 & KB loop 状态
    service._retriever = None
    with patch.object(service, "get_retriever", new=AsyncMock(return_value=fake)):
        for _ in range(3):
            ctx, stats = service.retrieve_knowledge_sync("task", "proj-1")
            assert "struct" in ctx
    # 三次调用都应在同一个 loop
    assert len(set(seen)) == 1, f"sync 调用跑在了多个 loop 上: {seen}"
    print("  ✅ 多次 sync 检索共享同一持久 KB loop")


def test_async_calls_from_different_loops_no_crossloop_error():
    """从两个不同的调用方 loop 调 async retrieve_knowledge，都不应报跨 loop 错误。"""
    from swarm.knowledge import service

    fake, seen = _make_fake_retriever()
    service._retriever = None

    with patch.object(service, "get_retriever", new=AsyncMock(return_value=fake)):
        # loop A
        async def call_once():
            ctx, _ = await service.retrieve_knowledge("task", "proj-1")
            return ctx

        ctx1 = asyncio.run(call_once())   # 第一个临时调用方 loop
        ctx2 = asyncio.run(call_once())   # 第二个临时调用方 loop（模拟重试 dispatch）
        assert "struct" in ctx1 and "struct" in ctx2

    # 实际检索都打到同一个 KB loop（与调用方 loop 无关）
    assert len(set(seen)) == 1, f"检索实际跑在了多个 loop 上: {seen}"
    print("  ✅ 跨调用方 loop 的 async 检索统一在 KB loop 执行，无跨 loop 崩溃")


def test_kb_loop_is_persistent_daemon():
    """KB loop 应是常驻守护线程，重复获取返回同一个 loop。"""
    from swarm.knowledge import service

    loop1 = service._get_kb_loop()
    loop2 = service._get_kb_loop()
    assert loop1 is loop2, "KB loop 应是单例"
    assert not loop1.is_closed(), "KB loop 不应关闭"
    print("  ✅ KB loop 常驻、单例、未关闭")


def test_empty_project_id_short_circuits():
    """空 project_id 直接返回空，不触达 KB loop / retriever。"""
    from swarm.knowledge import service

    ctx, stats = service.retrieve_knowledge_sync("task", "")
    assert ctx["struct"] == [] and stats == {}
    print("  ✅ 空 project_id 短路返回")


if __name__ == "__main__":
    test_kb_loop_is_persistent_daemon()
    test_empty_project_id_short_circuits()
    test_sync_calls_share_one_persistent_loop()
    test_async_calls_from_different_loops_no_crossloop_error()
    print("\n知识检索跨 loop 回归测试通过。")
