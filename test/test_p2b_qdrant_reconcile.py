#!/usr/bin/env python3
"""P2-B 回归：Qdrant 孤儿向量对账——删已不存在项目的残留 points。纯 mock client。"""

from __future__ import annotations

import inspect


class _Pt:
    def __init__(self, pid):
        self.payload = {"project_id": pid}


class _FakeClient:
    def __init__(self, pages):
        self._pages = pages  # list of (points, next_offset)
        self._i = 0
        self.deleted: list = []

    async def scroll(self, **kw):
        page = self._pages[self._i]
        self._i += 1
        return page

    async def delete(self, collection_name, points_selector):
        # 从 filter 里抠出 project_id
        f = points_selector.filter
        pid = f.must[0].match.value
        self.deleted.append(pid)


def _make_indexer(client):
    from swarm.knowledge.semantic_index import SemanticIndexer

    idx = SemanticIndexer.__new__(SemanticIndexer)
    idx._collection_name = "swarm_kb"
    idx._client = client
    idx._client_or_raise = lambda: client
    return idx


async def test_reconcile_deletes_only_orphans():
    """Qdrant 有 p1/p2/p3；存活集只 {p1} → 删 p2/p3，不碰 p1。"""
    client = _FakeClient([
        ([_Pt("p1"), _Pt("p2")], "off1"),
        ([_Pt("p3"), _Pt("p1")], None),  # None offset = 结束
    ])
    idx = _make_indexer(client)
    cleaned = await idx.reconcile_orphan_points({"p1"})
    assert cleaned == 2
    assert set(client.deleted) == {"p2", "p3"}
    assert "p1" not in client.deleted


async def test_reconcile_noop_when_all_live():
    client = _FakeClient([([_Pt("p1"), _Pt("p2")], None)])
    idx = _make_indexer(client)
    cleaned = await idx.reconcile_orphan_points({"p1", "p2"})
    assert cleaned == 0 and client.deleted == []


async def test_reconcile_empty_live_set_refuses_mass_delete():
    """★数据安全★：存活集为空(DB 读失败)→ 拒绝对账，绝不误删全量向量。"""
    client = _FakeClient([([_Pt("p1"), _Pt("p2")], None)])
    idx = _make_indexer(client)
    cleaned = await idx.reconcile_orphan_points(set())
    assert cleaned == 0 and client.deleted == [], "空存活集必须拒绝删除（防灾难）"


async def test_reconcile_scroll_failure_nonfatal():
    class _Boom:
        async def scroll(self, **kw):
            raise ConnectionError("qdrant down")

    idx = _make_indexer(_Boom())
    assert await idx.reconcile_orphan_points({"p1"}) == 0  # 不抛，返 0


def test_reconcile_wired_into_daily_scheduler():
    import sys
    import swarm.api.app  # noqa: F401
    appmod = sys.modules["swarm.api.app"]

    src = inspect.getsource(appmod._run_kb_prune_once)
    assert "reconcile_orphan_points" in src, "Qdrant 孤儿对账未接入每日调度（P2-B 回归）"
    # 复核 F1（TOCTOU）：reconcile 前就地重取 live 集，不用陈旧快照
    assert "fresh_projects" in src, "reconcile 前未重取 live 集（F1 TOCTOU 回归）"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
