"""主题I M-3（外部深审 MEDIUM）：Standalone 事件队列与归属映射永久残留（内存泄漏）。

病根：单跑完成后仅清 task handle，不清 _worker_queues / _worker_run_project → 历史结果长期
驻留（每个 run 一条，永不回收）；队列还无界（无订阅者时可无界堆积）。治：完成后延迟回收
（保留期供 SSE 读完终态）、有界队列（满丢最旧保最新含终态）、硬上界驱逐已完成最旧 run。
"""
from __future__ import annotations

import asyncio

import pytest

import swarm.worker.runner as wr


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    wr._worker_queues.clear()
    wr._worker_running.clear()
    wr._worker_run_project.clear()
    wr._worker_tasks.clear()
    yield
    wr._worker_queues.clear()
    wr._worker_running.clear()
    wr._worker_run_project.clear()
    wr._worker_tasks.clear()


def test_m3_queue_bounded_drops_oldest_keeps_newest(monkeypatch):
    """有界队列满 → 丢最旧、保最新（终态事件恒在队尾被保留），且 _emit 绝不阻塞。"""
    monkeypatch.setenv("SWARM_WORKER_RUN_QUEUE_MAX", "16")  # 下限 16

    async def _scenario():
        q = wr.register_worker_queue("r1")
        assert q.maxsize == 16, "队列必须有界"
        for i in range(40):
            await wr._emit(q, {"step": "log", "i": i})
        await wr._emit(q, {"step": "complete", "i": 999})
        # 队列不超过 maxsize，且尾部是最新（含终态）。
        assert q.qsize() <= 16
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
        assert drained[-1]["step"] == "complete", "终态事件必须被保留（丢最旧不丢最新）"
        assert drained[-1]["i"] == 999

    asyncio.run(_scenario())


def test_m3_cleanup_removes_queue_and_project_map():
    wr.register_worker_queue("r2")
    wr.register_worker_run_project("r2", "proj-x")
    assert "r2" in wr._worker_queues and "r2" in wr._worker_run_project
    wr._cleanup_worker_run("r2")
    assert "r2" not in wr._worker_queues, "完成后回收事件队列"
    assert "r2" not in wr._worker_run_project, "完成后回收归属映射（不永久滞留）"


def test_m3_schedule_cleanup_immediate_when_retention_zero(monkeypatch):
    monkeypatch.setenv("SWARM_WORKER_RUN_RETENTION_S", "0")

    async def _scenario():
        wr.register_worker_queue("r3")
        wr.register_worker_run_project("r3", "proj-y")
        wr._schedule_worker_run_cleanup("r3")  # retention=0 → 立即清
        assert "r3" not in wr._worker_queues
        assert "r3" not in wr._worker_run_project

    asyncio.run(_scenario())


def test_m3_schedule_cleanup_delayed_then_fires(monkeypatch):
    monkeypatch.setenv("SWARM_WORKER_RUN_RETENTION_S", "0.05")

    async def _scenario():
        wr.register_worker_queue("r4")
        wr.register_worker_run_project("r4", "proj-z")
        wr._schedule_worker_run_cleanup("r4")
        assert "r4" in wr._worker_queues, "保留期内仍在（供 SSE 读终态）"
        await asyncio.sleep(0.12)
        assert "r4" not in wr._worker_queues, "保留期到 → 回收"
        assert "r4" not in wr._worker_run_project

    asyncio.run(_scenario())


def test_m3_evict_stale_runs_never_evicts_running(monkeypatch):
    monkeypatch.setenv("SWARM_WORKER_RUN_MAX_RETAINED", "16")  # 下限 16
    # 填 20 个已完成 run + 标记 1 个在跑。
    for i in range(20):
        wr.register_worker_queue(f"done-{i}")
        wr.register_worker_run_project(f"done-{i}", "p")
    wr.register_worker_queue("live")
    wr._worker_running.add("live")
    wr._evict_stale_runs()  # 超 16 → 驱逐最旧的已完成项
    assert len(wr._worker_queues) <= 16, "超上界必须驱逐（硬兜底防撑爆）"
    assert "live" in wr._worker_queues, "绝不驱逐在跑的 run"


if __name__ == "__main__":
    print("run via pytest")
