"""R65-T2: 项目知识层外科清理（purge_project_knowledge）单测。

round65 实锤：E2E 每轮复用同 project_id，失败轮 worker 产物经 DONE 后增量回灌
（dispatch._feedback_to_knowledge）进 PG kb_* + Qdrant，而基线重置只 git reset 磁盘
→ 幻影模块知识跨轮堆叠（Qdrant 540 alarm 点/PG 286 alarm 符号/20+ 种冲突布局），
污染下一轮规划检索。治本=外科清理：只清【文件事实/行为】知识层（kb_*），
保留 projects 注册行、task_records 历史、mem_* 经验层（L2/L5/L6 跨轮学习价值 +
前提证伪机制自会淘汰过期经验）。Qdrant 由调用方配对清理（脚本入口）。
"""
from __future__ import annotations

import pytest

from swarm.project import store


class _FakeCursor:
    """记录 execute 的 SQL；to_regclass 按 missing_tables 集合应答。"""

    def __init__(self, missing_tables: set[str]):
        self.missing = missing_tables
        self.executed: list[tuple[str, tuple]] = []
        self._last_regclass_exists = True
        self.rowcount = 0
        self._rows_by_table = {}

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        if "to_regclass" in sql:
            self._last_regclass_exists = params[0] not in self.missing
        elif sql.startswith("DELETE"):
            self.rowcount = self._rows_by_table.get(self._table_of(sql), 3)

    @staticmethod
    def _table_of(sql: str) -> str:
        return sql.split("FROM", 1)[1].split()[0]

    def fetchone(self):
        return (1,) if self._last_regclass_exists else (None,)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patched(monkeypatch, missing: set[str] | None = None) -> _FakeCursor:
    cur = _FakeCursor(missing or set())
    monkeypatch.setattr(store, "_get_conn", lambda conn_str=None: _FakeConn(cur))
    return cur


def _deleted_tables(cur: _FakeCursor) -> set[str]:
    return {_FakeCursor._table_of(sql) for sql, _ in cur.executed if sql.startswith("DELETE")}


def test_purge_clears_all_kb_tables_and_only_kb(monkeypatch):
    """全部 kb_* 知识表按 project_id 参数化清理；mem_*/projects/task_records 绝不触碰。"""
    cur = _patched(monkeypatch)
    counts = store.purge_project_knowledge("pid-1")
    deleted = _deleted_tables(cur)
    expected = {"kb_file_index", "kb_symbol_index", "kb_dependency_graph", "kb_norms",
                "kb_modification_log", "kb_co_occurrence", "kb_update_events",
                "kb_pending_embeddings", "kb_mr_history"}
    assert deleted == expected, f"清理面漂移: {deleted ^ expected}"
    # 经验层/注册行/任务史是保留红线
    for sql, params in cur.executed:
        if sql.startswith("DELETE"):
            tbl = _FakeCursor._table_of(sql)
            assert not tbl.startswith("mem_"), f"经验层被误清: {tbl}"
            assert tbl not in ("projects", "task_records"), f"注册/历史被误清: {tbl}"
            assert params == ("pid-1",), f"必须按 project_id 参数化: {sql}"
    assert set(counts) == expected and all(v == 3 for v in counts.values())


def test_purge_table_list_single_source_with_delete_project():
    """清理面与 delete_project 级联共用单一事实源常量（round63 冻结纪律：
    不正则扫源码，直接断言两处引用同一常量对象）。"""
    assert isinstance(store._KB_KNOWLEDGE_TABLES, tuple)
    assert "kb_mr_history" in store._KB_KNOWLEDGE_TABLES
    assert not any(t.startswith("mem_") for t in store._KB_KNOWLEDGE_TABLES)


def test_purge_skips_missing_tables_without_error(monkeypatch):
    """迁移未跑/表不存在 → 跳过该表不报错（不让 undefined_table 回滚整个事务）。"""
    cur = _patched(monkeypatch, missing={"kb_mr_history", "kb_pending_embeddings"})
    counts = store.purge_project_knowledge("pid-2")
    deleted = _deleted_tables(cur)
    assert "kb_mr_history" not in deleted and "kb_pending_embeddings" not in deleted
    assert "kb_file_index" in deleted
    assert "kb_mr_history" not in counts


def test_purge_rejects_empty_project_id(monkeypatch):
    """空 project_id = 无 WHERE 全表清空的前奏，必须拒绝（默认拒绝铁律）。"""
    _patched(monkeypatch)
    with pytest.raises(ValueError):
        store.purge_project_knowledge("")
    with pytest.raises(ValueError):
        store.purge_project_knowledge(None)  # type: ignore[arg-type]


# ────────── 对抗双复核整改锁（R65-T2 CRITICAL 键名/legacy 集合/单一事实源）──────────

def _load_purge_script():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "scripts" / "e2e_purge_project_knowledge.py"
    spec = importlib.util.spec_from_file_location("e2e_purge_kb_script", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_preprocess_status_parses_real_response_shape():
    """复核 CRITICAL 整改锁：轮询必须读端点真实键 project_status（api/routers/
    project.py preprocess/status 返回 {"project_status","progress"}），终态=READY/ERROR
    （preprocess.py 写入词表）。读错键=永远不识别完成→每轮烧满超时硬退（狼来了闸）。"""
    mod = _load_purge_script()
    # 端点真实形状（成功/失败/进行中）
    assert mod._parse_preprocess_status({"project_status": "READY", "progress": {}}) == "READY"
    assert mod._parse_preprocess_status({"project_status": "ERROR", "progress": {}}) == "ERROR"
    assert mod._parse_preprocess_status({"project_status": "PREPROCESSING"}) == "PREPROCESSING"
    # 回归锁：旧实现读的错误键绝不能再被识别为终态
    assert mod._parse_preprocess_status({"status": "READY"}) == ""
    assert mod._parse_preprocess_status({"graph_status": "READY"}) == ""
    assert mod._parse_preprocess_status({}) == ""
    assert mod._parse_preprocess_status(None) == ""


def test_delete_project_references_single_source_constant():
    """复核 LOW 整改：真正钉住 delete_project 引用常量本体（此前 docstring 超卖）。
    仓库先例 test_p2a_purge_cascade 同款 getsource 钉级联清单。"""
    import inspect
    src = inspect.getsource(store.delete_project)
    assert "_KB_KNOWLEDGE_TABLES" in src, \
        "delete_project 不再引用单一事实源常量——清理面将与 purge_project_knowledge 漂移"
    # 不允许倒退回字面量表清单
    assert '"kb_file_index"' not in src, "kb 表清单不得在 delete_project 内重新字面量化"


def test_qdrant_delete_by_project_also_kills_legacy_collection():
    """猎手 (b) 整改锁：search() 的 project_<id> 旧集合回退会在共享集合清空后
    复活 legacy 残留——delete_by_project 必须成对清掉 legacy 集合。"""
    import asyncio
    from unittest.mock import AsyncMock
    from swarm.knowledge.semantic_index import SemanticIndexer

    idx = SemanticIndexer()
    client = AsyncMock()
    idx._client = client

    class _Col:
        def __init__(self, name):
            self.name = name

    class _Cols:
        collections = [_Col("swarm_kb"), _Col("project_pid-9")]

    client.get_collections.return_value = _Cols()
    asyncio.run(idx.delete_by_project("pid-9"))
    client.delete.assert_awaited()  # 共享集合按 project_id 过滤删
    client.delete_collection.assert_awaited_once_with(collection_name="project_pid-9")

    # 无 legacy 集合时绝不误删
    client2 = AsyncMock()
    idx._client = client2

    class _Cols2:
        collections = [_Col("swarm_kb")]

    client2.get_collections.return_value = _Cols2()
    asyncio.run(idx.delete_by_project("pid-9"))
    client2.delete_collection.assert_not_awaited()
