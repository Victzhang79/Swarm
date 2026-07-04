"""round23 审计治本 — 存储层（连接/事务一致性）。

storage#1：KnowledgeUpdater.close() 未置空 _conn → connect() 幂等守卫复用已关连接。
（storage#2 preprocess 符号索引、L2 window 事务化、storage#6 embedding 重试 retry_count 见实现，
  DB 集成路径由既有 knowledge/memory 测试回归护住。）
"""
from __future__ import annotations

import asyncio


def test_updater_close_nulls_connection_refs():
    from swarm.knowledge.updater import KnowledgeUpdater

    u = KnowledgeUpdater.__new__(KnowledgeUpdater)
    u._depgraph_tasks = set()
    u._struct = u._semantic = u._behavior = None
    closed = {"v": False}

    class _Conn:
        async def close(self):
            closed["v"] = True

    u._conn = _Conn()
    asyncio.run(u.close())
    assert closed["v"] is True, "底层连接应被关闭"
    assert u._conn is None, "close() 后 _conn 必须置空（否则 connect() 复用已关连接）"
    assert u._struct is None and u._semantic is None and u._behavior is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
