"""R65B-T2: preprocess 语义层源码全文嵌入（干净重建后召回塌陷治本）。

T2 知识层 purge 曝光的先天缺口：preprocess 只嵌【符号签名】元数据向量，源码全文
切块历史上只有增量 updater（worker DONE 回灌）产出——语义层质量靠失败轮碰文件
顺带长出（连同幻影一起）。purge+重建后源文本类查询召回塌（Recall@5 0.75+→0.568）。

治本设计（含对抗复核整改）：
- 与增量完全同一管线：逐文件 reindex_file_atomic（file-scoped write-then-prune）。
  猎手(a)：绝不做项目级代际 prune——会把并发 updater 刚写的新鲜 chunk 连带删掉；
- 猎手(b) 异常分类：嵌入服务级故障（Unavailable/DimensionMismatch/嵌入 None）整段中止；
  单文件病理（tree-sitter 爆栈等）跳过计数继续，占比超阈值才判服务异常；
- 猎手(d)：文件清单 DB 读失败 ≠ 空项目——机读 aborted 标记；
- 资产/三方件排除（栈中立目录约定）：static/vendored/minified 不入语义嵌入
  （实测 135/624 个 vendored JS 把业务源码挤出 top5）；
- 猎手(c)：readiness 把 source_aborted 归 partial；purge 脚本 READY 后核 embed_stats。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from swarm.project import preprocess as pp


class _FakeIndexer:
    """替身：记录 reindex_file_atomic 调用，不连 Qdrant。"""

    def __init__(self):
        self.reindexed: list[str] = []
        self.embed_fn = None
        self.closed = False
        self.fail_on: dict[str, BaseException] = {}

    async def connect(self):
        return None

    def set_embed_fn(self, fn):
        self.embed_fn = fn

    async def reindex_file_atomic(self, project_id, source, file_path, module_name=None):
        exc = self.fail_on.get(file_path)
        if exc is not None:
            raise exc
        self.reindexed.append(file_path)
        return 2  # 每文件 2 chunk

    async def close(self):
        self.closed = True


@pytest.fixture()
def proj(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.java").write_text("class A { void run() {} }")
    (tmp_path / "src" / "B.py").write_text("def b():\n    return 1\n")
    (tmp_path / "big.sql").write_text("x" * 600_000)  # 超上限跳过
    fake = _FakeIndexer()
    monkeypatch.setattr(pp, "_make_semantic_indexer", lambda: fake)
    monkeypatch.setattr(
        pp, "_read_files_for_source_embed",
        lambda pid: ["src/A.java", "src/B.py", "big.sql", "gone/Missing.java"])
    monkeypatch.setattr(pp, "_embed_texts", lambda texts: [[0.5] * 4 for _ in texts])
    return tmp_path, fake


def test_source_pass_reindexes_each_file_atomically(proj):
    """逐文件 reindex（file-scoped write-then-prune，与增量 updater 同语义）；
    超大/缺失文件跳过计数。"""
    tmp, fake = proj
    stats = asyncio.run(pp._embed_source_text_chunks("pid-1", str(tmp)))
    assert fake.reindexed == ["src/A.java", "src/B.py"]
    assert stats == {"files": 2, "chunks": 4, "skipped": 2, "failed_files": 0}
    assert fake.closed


def test_service_level_failure_aborts_pass(proj):
    """嵌入服务级故障（EmbeddingUnavailableError，类型判据非猜字符串）→ 整段中止，
    机读 aborted；已写文件各自完成 file-scoped prune 无空窗。"""
    from swarm.knowledge.semantic_index import EmbeddingUnavailableError
    tmp, fake = proj
    fake.fail_on = {"src/B.py": EmbeddingUnavailableError("embedding 服务不可用")}
    stats = asyncio.run(pp._embed_source_text_chunks("pid-2", str(tmp)))
    assert stats["files"] == 1 and stats.get("aborted")
    assert fake.closed


def test_single_poisoned_file_skips_not_aborts(proj):
    """猎手(b)：单文件病理（如 tree-sitter RecursionError）跳过继续，不连坐全程。"""
    tmp, fake = proj
    fake.fail_on = {"src/A.java": RecursionError("pathological file")}
    stats = asyncio.run(pp._embed_source_text_chunks("pid-3", str(tmp)))
    assert fake.reindexed == ["src/B.py"], "坏文件后续文件必须继续处理"
    assert stats["failed_files"] == 1 and not stats.get("aborted")


def test_cancel_event_aborts(proj, monkeypatch):
    tmp, fake = proj
    import threading
    ev = threading.Event()
    ev.set()
    monkeypatch.setattr(pp, "_get_cancel_event", lambda pid: ev)
    stats = asyncio.run(pp._embed_source_text_chunks("pid-4", str(tmp)))
    assert fake.reindexed == [] and stats.get("aborted") == "cancelled"


def test_file_list_db_failure_is_machine_readable(proj, monkeypatch):
    """猎手(d)：清单读失败绝不折叠成"空项目"——必须带 aborted 机读标记。"""
    tmp, _fake = proj

    def _boom(pid):
        raise ConnectionError("pg down")

    monkeypatch.setattr(pp, "_read_files_for_source_embed", _boom)
    stats = asyncio.run(pp._embed_source_text_chunks("pid-5", str(tmp)))
    assert "file_list_read_failed" in (stats.get("aborted") or "")


def test_vendored_static_minified_excluded():
    """资产/三方件排除（栈中立目录约定）：static/vendored/minified 不入语义嵌入，
    但前端项目的一等源码（src 下 .js/.html）保留。"""
    ex = pp._source_embed_excluded
    assert ex("ruoyi-admin/src/main/resources/static/ajax/libs/x/y.js")
    assert ex("web/node_modules/react/index.js")
    assert ex("app/vendor/lib.rb")
    assert ex("assets/app.min.js") and ex("dist/app.js.map")
    assert not ex("web/src/App.js"), "前端一等源码绝不排除（栈中立）"
    assert not ex("templates/index.html")
    assert not ex("src/main/java/com/x/PageUtils.java")


def test_phase_embed_wires_source_pass(monkeypatch):
    """_phase_embed 在符号向量落盘成功后必须执行源码全文嵌入并合并统计。"""
    calls = {}

    async def _fake_src(pid, ppath, progress_cb=None):
        calls["src"] = (pid, ppath)
        return {"files": 3, "chunks": 30, "skipped": 0, "failed_files": 0}

    monkeypatch.setattr(pp, "_embed_source_text_chunks", _fake_src)
    monkeypatch.setattr(pp, "_check_qdrant", lambda: True)
    monkeypatch.setattr(pp, "_read_symbols_for_embed", lambda pid: [
        {"file_path": "a.py", "name": "f", "symbol_type": "fn", "start_line": 1,
         "end_line": 2, "signature": "def f()", "docstring": "", "class_name": ""}])
    monkeypatch.setattr(pp, "_embed_texts", lambda texts: [[0.5] * 1024 for _ in texts])
    monkeypatch.setattr(pp, "_store_vectors_qdrant", lambda *a, **k: None)
    monkeypatch.setattr("swarm.project.store.upsert_progress", lambda *a, **k: None)

    out = asyncio.run(pp._phase_embed("pid-9", "/tmp/x", {}))
    assert calls["src"] == ("pid-9", "/tmp/x")
    assert out.get("source_files") == 3 and out.get("source_chunks") == 30


def test_readiness_partial_on_source_aborted():
    """猎手(c)：源码嵌入中止 → readiness=partial（绝不报 ready 让 Brain 蒙在鼓里）。"""
    from swarm.knowledge.readiness import assess_knowledge_readiness
    out = assess_knowledge_readiness(
        {"status": "READY"},
        {
            "phase": "complete",
            "index_stats": {"symbols": 100},
            "embed_stats": {"vectors": 100, "source_aborted": "embedding down",
                            "source_files": 1},
        })
    assert out["level"] == "partial", out
    assert "源码语义嵌入中止" in out["message"]


def test_purge_script_gate_rejects_degraded_source_layer(monkeypatch):
    """猎手(c)：purge 脚本 READY 后核 embed_stats——source_aborted/0 文件拒绝放行。"""
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "scripts" / "e2e_purge_project_knowledge.py"
    spec = importlib.util.spec_from_file_location("e2e_purge_kb_script2", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import swarm.project.store as store_mod
    monkeypatch.setattr(store_mod, "get_progress", lambda pid: {
        "embed_stats": {"vectors": 10, "source_aborted": "boom", "source_files": 5}})
    ok, why = mod._check_source_embed_stats("pid-x")
    assert not ok and "source_aborted" in why

    monkeypatch.setattr(store_mod, "get_progress", lambda pid: {
        "embed_stats": {"vectors": 10, "source_files": 0}})
    ok, why = mod._check_source_embed_stats("pid-x")
    assert not ok and "source_files=0" in why

    monkeypatch.setattr(store_mod, "get_progress", lambda pid: {
        "embed_stats": {"vectors": 10, "source_files": 480, "source_chunks": 9000}})
    ok, why = mod._check_source_embed_stats("pid-x")
    assert ok and "480" in why
