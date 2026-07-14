"""R54-3 治本锁：知识库增量回灌链路（round54 实锤：从未成功过一次）。

死因：线上真表 `kb_update_events.event_type` 是 NOT NULL 且**无默认值**（schema 漂移——表早已
存在，代码里的 `CREATE TABLE IF NOT EXISTS ... DEFAULT 'push'` 从未生效），而 INSERT 不写该列
→ 每次入队 NotNullViolation → 被 `logger.debug` 静默吞 → kb_update_events / kb_modification_log /
kb_co_occurrence 三表恒空 → `retrieve_for_brain` 的 behavior 面五轮恒 0。
更坏的是：日志在 `create_task` 之后就抢先打 "已入队" INFO，**宣称成功、实际每次都失败**。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from swarm.knowledge.updater import ChangeType, FileChange, KnowledgeUpdater, UpdateEvent


@pytest.mark.asyncio
async def test_enqueue_writes_event_type_explicitly():
    """★ INSERT 必须显式写 event_type ★ —— 绝不依赖 schema 默认值（线上真表就没有默认值）。"""
    u = KnowledgeUpdater.__new__(KnowledgeUpdater)
    cur = AsyncMock()
    cur.fetchone = AsyncMock(return_value=(42,))
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=_actx(cur))
    u._conn = conn
    u._lock = asyncio.Lock()

    ev = UpdateEvent(project_id="p1", task_id="st-1",
                     changes=[FileChange(file_path="a/pom.xml", change_type=ChangeType.ADDED)],
                     metadata={"source": "worker_feedback"})
    assert await u.enqueue_event(ev) == 42
    sql, params = cur.execute.await_args.args
    assert "event_type" in sql, "★ INSERT 不写 event_type → 线上 NotNullViolation，整条回灌链路死掉"
    assert params[1] == "worker_feedback", "event_type 取自事件语义（metadata.source）"


class _actx:
    def __init__(self, v):
        self._v = v

    async def __aenter__(self):
        return self._v

    async def __aexit__(self, *a):
        return False
