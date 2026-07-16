"""P0-7 / D13 回归：Qdrant point ID 必须含 project_id——跨项目同路径 chunk 不得互相覆盖。

机制（DEEP_READ_REGISTER_2026-07-07.md D13）：
全项目共用单集合 swarm_kb，旧口径 make_point_id = uuid5(file_path:start_line) 不含
project_id → 项目 A/B 同相对路径同起始行（pom.xml:1 / README:1 几乎必然）互相 upsert
同一 point ID，payload.project_id 被最后写者替换 → 先写项目该 chunk 检索静默消失，
且其带 project_id 过滤的 prune/delete 无法回收该点。

本文件全部为行为断言（纯内存 fake Qdrant client），不依赖真 Qdrant 服务，
不做任何源码字符串断言。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

DIM = 4  # 测试用小维度（indexer._dim 同步设 4，避免造 1024 维向量）


# ──────────────────────────────────────────────
# 纯内存 fake AsyncQdrantClient（仅实现本测试触达的面）
# ──────────────────────────────────────────────

def _cond_matches(payload: dict, cond) -> bool:
    """FieldCondition(key, MatchValue) 匹配。"""
    return payload.get(cond.key) == cond.match.value


def _filter_matches(payload: dict, flt) -> bool:
    for c in getattr(flt, "must", None) or []:
        if not _cond_matches(payload, c):
            return False
    for c in getattr(flt, "must_not", None) or []:
        if _cond_matches(payload, c):
            return False
    return True


class FakeAsyncQdrant:
    def __init__(self):
        self.points: dict[str, dict] = {}  # id -> {"vector":…, "payload":…}
        self.upsert_calls = 0

    async def upsert(self, collection_name, points):
        self.upsert_calls += 1
        for p in points:
            self.points[str(p.id)] = {"vector": p.vector, "payload": dict(p.payload)}

    async def delete(self, collection_name, points_selector):
        flt = points_selector.filter
        doomed = [pid for pid, rec in self.points.items()
                  if _filter_matches(rec["payload"], flt)]
        for pid in doomed:
            del self.points[pid]

    async def get_collections(self):
        # R65-T2 起 delete_by_project 会探测 legacy project_<id> 集合——替身只模拟
        # 单一共享集合，返回空清单即可（无 legacy 集合分支）
        class _Cols:
            collections = []
        return _Cols()

    async def delete_collection(self, collection_name):
        raise AssertionError("单集合替身不应发生集合级删除")

    # ── 便捷查询（测试用，非 Qdrant API）──
    def payloads_for_project(self, project_id: str) -> list[dict]:
        return [rec["payload"] for rec in self.points.values()
                if rec["payload"].get("project_id") == project_id]


async def _embed_stub(texts):
    return [[0.5] + [0.1] * (DIM - 1) for _ in texts]


def _make_indexer(client: FakeAsyncQdrant):
    from swarm.knowledge.semantic_index import SemanticIndexer

    idx = SemanticIndexer.__new__(SemanticIndexer)
    idx._collection_name = "swarm_kb"
    idx._client = client
    idx._dim = DIM
    idx._kb_config = SimpleNamespace(
        chunk_size=512, chunk_overlap=50, retrieval_top_k=5, rerank_top_k=3,
    )
    idx._embed_fn = _embed_stub
    return idx


def _chunk(file_path: str, start_line: int, content: str):
    from swarm.knowledge.semantic_index import Chunk
    return Chunk(
        content=content, chunk_type="free_text", file_path=file_path,
        start_line=start_line, end_line=start_line,
    )


# ──────────────────────────────────────────────
# make_point_id 新口径：project_id 参与 key
# ──────────────────────────────────────────────

def test_point_id_differs_across_projects_same_path():
    """同 (file_path, start_line) 不同 project → 必须产不同 point ID（D13 核心）。"""
    from swarm.knowledge.semantic_index import make_point_id

    a = make_point_id(project_id="proj-a", file_path="pom.xml", start_line=1)
    b = make_point_id(project_id="proj-b", file_path="pom.xml", start_line=1)
    assert a != b, "跨项目同路径同起始行必须产不同 point ID"
    assert isinstance(a, str) and len(a) == 36  # 仍是 uuid5 字符串（Qdrant 合法 ID）


def test_point_id_stable_and_content_agnostic_within_project():
    """同一项目内保持 A-P1-19 语义：同 (project,file,line) 恒同 ID、content 不参与。"""
    from swarm.knowledge.semantic_index import make_point_id

    a = make_point_id(project_id="p1", file_path="pkg/m.py", start_line=10,
                      content="def foo(): return 1")
    b = make_point_id(project_id="p1", file_path="pkg/m.py", start_line=10,
                      content="def bar(): return 2")
    assert a == b, "同项目同 (file,line) 内容不同仍须同 ID（codegraph/semantic 两路去重）"
    assert a != make_point_id(project_id="p1", file_path="pkg/m.py", start_line=11)


def test_point_id_fail_closed_on_missing_project_id():
    """project_id 缺失/为空 → 拒绝生成 ID（禁止静默退回旧口径混写）。"""
    from swarm.knowledge.semantic_index import make_point_id

    with pytest.raises(ValueError):
        make_point_id(project_id="", file_path="pom.xml", start_line=1)
    with pytest.raises(ValueError):
        make_point_id(project_id="   ", file_path="pom.xml", start_line=1)
    with pytest.raises(ValueError):
        make_point_id(project_id=None, file_path="pom.xml", start_line=1)  # type: ignore[arg-type]


# ──────────────────────────────────────────────
# 索引行为：跨项目不再互相覆盖
# ──────────────────────────────────────────────

async def test_cross_project_same_path_chunks_coexist():
    """项目 A 先索引 pom.xml:1，项目 B 再索引同路径同行 → A 的点必须原样存活。"""
    client = FakeAsyncQdrant()
    idx = _make_indexer(client)

    await idx.index_chunks("proj-a", [_chunk("pom.xml", 1, "<project>A</project>")])
    await idx.index_chunks("proj-b", [_chunk("pom.xml", 1, "<project>B</project>")])

    assert len(client.points) == 2, "两项目同路径 chunk 必须是两个独立 point"
    pa = client.payloads_for_project("proj-a")
    pb = client.payloads_for_project("proj-b")
    assert len(pa) == 1 and pa[0]["content"] == "<project>A</project>"
    assert len(pb) == 1 and pb[0]["content"] == "<project>B</project>"


async def test_same_project_reindex_still_dedupes():
    """同项目同 (file,line) 重复索引 → 仍幂等覆盖为单点（保住去重语义，不退化成堆积）。"""
    client = FakeAsyncQdrant()
    idx = _make_indexer(client)

    await idx.index_chunks("proj-a", [_chunk("pom.xml", 1, "v1")])
    await idx.index_chunks("proj-a", [_chunk("pom.xml", 1, "v2 updated")])

    assert len(client.points) == 1, "同项目同 (file,line) 必须幂等覆盖同一 point"
    assert client.payloads_for_project("proj-a")[0]["content"] == "v2 updated"


async def test_index_chunks_fail_closed_on_empty_project_id():
    """index_chunks 空 project_id → 拒绝索引（ValueError），零写入。"""
    client = FakeAsyncQdrant()
    idx = _make_indexer(client)

    with pytest.raises(ValueError):
        await idx.index_chunks("", [_chunk("pom.xml", 1, "x")])
    with pytest.raises(ValueError):
        await idx.index_chunks("   ", [_chunk("pom.xml", 1, "x")])
    assert client.points == {} and client.upsert_calls == 0, "fail-closed 必须发生在任何 upsert 之前"


# ──────────────────────────────────────────────
# 旧口径孤儿点收敛：现有 write-then-prune 按 payload 过滤删除，
# 必须能回收旧 ID（不含 project_id 的 uuid5）点。
# ──────────────────────────────────────────────

async def test_reindex_file_atomic_prunes_legacy_id_points():
    """预置旧口径 ID（uuid5(file:line)，无 project_id）的存量点 →
    同项目同文件 reindex_file_atomic 后：旧 ID 点被 prune 回收，新口径点就位。"""
    from swarm.knowledge.semantic_index import make_point_id

    client = FakeAsyncQdrant()
    idx = _make_indexer(client)

    src = "some file content line one\n"
    # 旧口径存量点：ID 不含 project_id，但 payload 完整（prune 按 payload 过滤应能命中）
    legacy_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "pom.xml:1"))
    client.points[legacy_id] = {
        "vector": [0.5] + [0.1] * (DIM - 1),
        "payload": {
            "project_id": "proj-a", "file_path": "pom.xml",
            "content": "stale", "index_generation": "old-gen",
        },
    }

    await idx.reindex_file_atomic("proj-a", src, "pom.xml")

    assert legacy_id not in client.points, "旧口径孤儿点必须被现有 prune（payload 过滤）回收"
    new_ids = [pid for pid, rec in client.points.items()
               if rec["payload"].get("project_id") == "proj-a"
               and rec["payload"].get("file_path") == "pom.xml"]
    assert new_ids, "新口径点必须已写入"
    assert all(nid == make_point_id(
        project_id="proj-a", file_path="pom.xml",
        start_line=client.points[nid]["payload"].get("start_line"),
    ) for nid in new_ids), "新写入点必须全部走含 project_id 的新口径 ID"


async def test_delete_by_project_recovers_legacy_id_points():
    """delete_by_project 按 payload.project_id 过滤 → 旧口径 ID 点同样可回收（无孤儿）。"""
    client = FakeAsyncQdrant()
    idx = _make_indexer(client)

    legacy_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "README.md:1"))
    client.points[legacy_id] = {
        "vector": [0.5] + [0.1] * (DIM - 1),
        "payload": {"project_id": "proj-a", "file_path": "README.md", "content": "x"},
    }
    await idx.index_chunks("proj-b", [_chunk("README.md", 1, "keep me")])

    await idx.delete_by_project("proj-a")

    assert legacy_id not in client.points, "旧口径点须能按 project_id payload 删除"
    assert len(client.payloads_for_project("proj-b")) == 1, "他项目点不得被误删"


# ──────────────────────────────────────────────
# 预处理全量路径（codegraph）与增量路径口径一致
# ──────────────────────────────────────────────

def test_codegraph_and_semantic_paths_share_new_scheme():
    """两条写入路径对同一 (project,file,line) 必须仍产同一 ID（互相幂等去重不回归），
    且对不同 project 产不同 ID。"""
    from swarm.knowledge.semantic_index import make_point_id

    codegraph_content = "def foo(a, b): ... | 求和 | foo"
    semantic_content = "def foo(a, b):\n    return a + b\n"
    same_proj_a = make_point_id(project_id="p", file_path="svc/x.py",
                                start_line=42, content=codegraph_content)
    same_proj_b = make_point_id(project_id="p", file_path="svc/x.py",
                                start_line=42, content=semantic_content)
    assert same_proj_a == same_proj_b

    other_proj = make_point_id(project_id="q", file_path="svc/x.py",
                               start_line=42, content=codegraph_content)
    assert same_proj_a != other_proj


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
