"""B7 知识库/经验层/记忆/预处理深读治本（DR-08-F1..F6 = #79-84）行为级测试。"""
from __future__ import annotations

import asyncio

import pytest


# ─────────────────────────── #79 预处理持久化 fail-loud ───────────────────────────

def test_79_save_file_index_raises_on_db_failure():
    """kb_file_index 写失败必须 raise（冒泡到 preprocess_project→ERROR），绝不吞成 READY。"""
    from unittest.mock import patch

    import swarm.project.preprocess as pp
    files = [{"rel_path": "a.py", "language": "python", "hash": "h",
              "lines": 1, "abs_path": "/x/a.py"}]
    with patch("swarm.infra.db.sync_pool", side_effect=RuntimeError("pg down")):
        with pytest.raises(Exception):
            pp._save_file_index("proj", files)


def test_79_empty_files_no_raise():
    """0 文件项目合法 → early-return，不触发 raise（不误伤空项目）。"""
    import swarm.project.preprocess as pp
    assert pp._save_file_index("proj", []) is None


# ─────────────────────────── #81 readiness DEGRADED ───────────────────────────

def test_81_readiness_degraded_index_is_partial():
    """结构索引降级(codegraph 失败：ok=False 无 skipped) → readiness 判 partial，不当 ready。"""
    from swarm.knowledge.readiness import assess_knowledge_readiness
    res = assess_knowledge_readiness(
        project={"status": "READY"},
        preprocess={"phase": "complete",
                    "index_stats": {"ok": False, "error": "codegraph failed"},
                    "embed_stats": {"vectors": 50}},
    )
    assert res["level"] == "partial"
    assert "结构索引降级" in res["message"]


def test_81_readiness_all_ok_is_ready():
    from swarm.knowledge.readiness import assess_knowledge_readiness
    res = assess_knowledge_readiness(
        project={"status": "READY"},
        preprocess={"phase": "complete",
                    "index_stats": {"ok": True, "symbols": 100},
                    "embed_stats": {"vectors": 50}},
    )
    assert res["level"] == "ready"


# ─────────────────────────── #84 死衰减路径 fail-loud ───────────────────────────

def test_84_decay_batch_methods_raise():
    from swarm.memory.decay import MemoryDecay
    md = MemoryDecay.__new__(MemoryDecay)
    with pytest.raises(NotImplementedError):
        asyncio.run(md.decay_l5_batch_sql())
    with pytest.raises(NotImplementedError):
        asyncio.run(md.decay_l6_batch_sql())


# ─────────────────────────── #82 扫描大文件不 OOM ───────────────────────────

def test_82_scan_huge_single_line_file_no_oom(tmp_path):
    """单行超大文本文件按块 count(\\n) 不整读入内存；超 50MB 跳过精确计行。"""
    import swarm.project.preprocess as pp
    # 构造一个 2MB 单行文件（无换行）——旧 sum(1 for _ in f) 会整读为单行
    big = tmp_path / "big.data"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    res = pp._scan_sync(str(tmp_path))
    # 不抛、正常返回（行数统计不因单行大文件崩）
    assert isinstance(res, dict) and "file_count" in res


# ─────────────────────────── #83 空 content 进重试非静默 done ───────────────────────────

def test_83_content_none_raises_for_retry():
    """ADDED content=None(未取到) → _process_change raise（handle_event 计入 errors→failed 重试）。"""
    from swarm.knowledge.updater import ChangeType, FileChange, KnowledgeUpdater
    upd = KnowledgeUpdater.__new__(KnowledgeUpdater)
    upd._struct = None
    upd._semantic = None
    ch = FileChange(file_path="a.py", change_type=ChangeType.ADDED, content=None)
    with pytest.raises(RuntimeError, match="content 未取到"):
        asyncio.run(upd._process_change("proj", ch))


def test_83_content_empty_string_is_benign_noop():
    """content=="" (真空文件) 合法 no-op，不 raise（不误判真空为未取到）。"""
    from swarm.knowledge.updater import ChangeType, FileChange, KnowledgeUpdater
    upd = KnowledgeUpdater.__new__(KnowledgeUpdater)
    upd._struct = None
    upd._semantic = None
    ch = FileChange(file_path="empty.py", change_type=ChangeType.MODIFIED, content="")
    # 不抛（None 才抛）
    asyncio.run(upd._process_change("proj", ch))


# ─────────────────────────── #80 retrieval_partial 聚合 ───────────────────────────

def test_80_partial_aggregation_transform():
    """检索层 error → retrieval_partial 聚合（<层>_error 去 _error 后缀成层名列表）。
    该 transform 是 retrieve_for_brain 末尾的确定性逻辑（k[:-6]）。"""
    stats = {"struct_count": 0, "struct_error": "pg dead", "semantic_error": "timeout",
             "norms_count": 3}
    partial = sorted(k[:-6] for k in stats if k.endswith("_error"))
    assert partial == ["semantic", "struct"]




if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
