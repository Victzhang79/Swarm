"""P1-25 回归：全量 preprocess 符号索引须清理已删符号/已删文件（非纯 upsert）。

- _save_symbol_index 对重新索引的文件 delete-then-insert → 文件内被删符号不残留。
- _prune_absent_files 对账 → 磁盘已不存在的文件的符号行被清除。

触真实 PG，_test_ 前缀隔离 + try/finally 清理。需本地 PG。
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from swarm.config.settings import DatabaseConfig
from swarm.knowledge.structure_index import StructureIndexer  # noqa: F401  (确保 DDL 建表)
from swarm.project.preprocess import _prune_absent_files, _save_symbol_index


def _pg_available() -> bool:
    try:
        with psycopg.connect(DatabaseConfig().postgres_uri, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="PG 不可达")

_PID = f"_test_p1_25_{uuid.uuid4().hex[:8]}"


def _conn():
    return psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True)


def _sym(file_path: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        file_path=file_path, name=name, symbol_type="function",
        start_line=1, end_line=2, signature=f"def {name}()", docstring=None, class_name=None,
    )


def _symbols(cur) -> set[tuple[str, str]]:
    cur.execute(
        "SELECT file_path, symbol_name FROM kb_symbol_index WHERE project_id = %s", (_PID,)
    )
    return {(r[0], r[1]) for r in cur.fetchall()}


def _ensure_tables():
    import asyncio
    idx = StructureIndexer()

    async def _mk():
        await idx.connect()
        await idx.ensure_tables()
        await idx.close()
    asyncio.run(_mk())


def test_preprocess_symbol_reconcile():
    _ensure_tables()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "fileA.py").write_text("def symbol2(): pass\n")  # 仍存在
        # fileGone.py 故意不创建 → 磁盘不存在

        try:
            # 预置：fileA 旧符号 symbol1、已删文件 fileGone 的符号
            with _conn() as conn, conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO kb_symbol_index "
                    "(project_id, file_path, symbol_name, symbol_type) VALUES (%s,%s,%s,%s)",
                    [
                        (_PID, "fileA.py", "symbol1", "function"),
                        (_PID, "fileGone.py", "symbolG", "function"),
                    ],
                )

            # 全量重索引：fileA 现在只产 symbol2（symbol1 已从文件删除）
            _save_symbol_index(_PID, [_sym("fileA.py", "symbol2")])
            # 对账磁盘：fileGone.py 不存在 → 清除
            pruned = _prune_absent_files(_PID, str(base))

            with _conn() as conn, conn.cursor() as cur:
                syms = _symbols(cur)

            assert ("fileA.py", "symbol1") not in syms, "文件内被删符号未清除(纯 upsert 回归)"
            assert ("fileA.py", "symbol2") in syms, "重新索引的符号应存在"
            assert ("fileGone.py", "symbolG") not in syms, "已删文件的符号未清除"
            assert pruned == 1
        finally:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM kb_symbol_index WHERE project_id = %s", (_PID,))
                cur.execute("DELETE FROM kb_dependency_graph WHERE project_id = %s", (_PID,))


def test_prune_guards_against_missing_project_dir():
    """project_path 不是现存目录时【绝不】对账——否则整表被误清空(fail-open mass-wipe)。"""
    _ensure_tables()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kb_symbol_index "
                "(project_id, file_path, symbol_name, symbol_type) VALUES (%s,%s,%s,%s)",
                (_PID, "keep.py", "s", "function"),
            )
        # 目录不存在 → 应短路返回 0，且不删任何行
        assert _prune_absent_files(_PID, "/tmp/_test_p1_25_does_not_exist_zzz") == 0
        assert _prune_absent_files(_PID, "") == 0
        with _conn() as conn, conn.cursor() as cur:
            assert ("keep.py", "s") in _symbols(cur), "现存目录缺失时误删了权威索引"
    finally:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM kb_symbol_index WHERE project_id = %s", (_PID,))
