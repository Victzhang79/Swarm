"""对抗复核（code-reviewer + silent-failure-hunter）确认项的回归测试。

覆盖：
- SF-1：_record_embed_usage 记账异常不得拖垮 embed 主结果（含 float usage 触发 int() 的场景）。
- B7/CR-3：learn 落库在 acquire 等待期被 cancel → 连接从未建立（无泄漏）。
- CR-1：run_task/resume_task/resume_planning 三入口 finally 均清理 per-task token 计数（防残留+泄漏）。
"""
from __future__ import annotations

import asyncio
import inspect

from unittest.mock import patch

from swarm.knowledge import embed_client


# ── SF-1 ──
def test_record_embed_usage_never_raises_on_bad_usage():
    # float prompt_tokens 旧代码 int("50.5") 抛 ValueError → 被外层吞成 return None 丢 embeddings。
    embed_client._record_embed_usage("m", "http://x", ["hello"], {"prompt_tokens": 50.5})  # 不抛即通过
    embed_client._record_embed_usage("m", "http://x", ["hello"], {"prompt_tokens": "bad"})
    embed_client._record_embed_usage("m", "http://x", ["hello"], None)


def test_record_embed_usage_records_float_as_int():
    with patch("swarm.models.usage_tracker.record") as rec:
        embed_client._record_embed_usage("m", "http://x", ["hello"], {"prompt_tokens": 50.9})
    assert rec.called
    assert rec.call_args.kwargs.get("prompt_tokens") == 50  # int(float(50.9))


# ── B7 / CR-3 ──
def test_learn_persist_no_connection_when_cancelled_at_acquire():
    """锁被占用时 persist 阻塞在 acquire；此时 cancel → MemoryStore 从未实例化(无连接泄漏)。"""
    from swarm.brain import learn_store

    created = {"n": 0}

    class _NeverStore:
        def __init__(self):
            created["n"] += 1

    async def _scenario():
        # _persist_lock 是模块级 asyncio.Lock，会绑定首个使用它的事件循环；每个 asyncio.run 是
        # 新循环，故测试内重绑到当前循环（生产单循环无此问题）。
        learn_store._persist_lock = asyncio.Lock()
        await learn_store._persist_lock.acquire()  # 先占锁，逼后续 persist 阻塞在 acquire
        try:
            with patch.object(learn_store, "MemoryStore", _NeverStore), \
                 patch.object(learn_store, "build_mistake_payload",
                              lambda *a, **k: {"error_type": "E", "description": "D"}), \
                 patch.object(learn_store, "build_l2_summary",
                              lambda *a, **k: {"summary": "S", "metadata": {}}):
                task = asyncio.create_task(
                    learn_store.persist_learn_failure({"project_id": "p", "task_id": "t"}, {}))
                await asyncio.sleep(0.05)  # 让它跑到 acquire 并阻塞
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            learn_store._persist_lock.release()

    asyncio.run(_scenario())
    assert created["n"] == 0, "cancel 发生在 acquire 等待期 → 连接不应被创建（无泄漏）"


# ── CR-1 ──
def test_all_task_entrypoints_clear_token_counter():
    """完整性不变量：所有经 _stream_brain_events(set_current_task) 的入口 finally 都必须清计数。"""
    from swarm.brain import runner
    for fn in (runner.run_task, runner.resume_task, runner.resume_planning):
        src = inspect.getsource(fn)
        assert "clear_task_total" in src, f"{fn.__name__} finally 缺 per-task token 清理(CR-1 泄漏/误杀)"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
