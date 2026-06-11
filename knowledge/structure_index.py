"""Layer A — 结构索引: 符号表 + 依赖图，PostgreSQL 存储

负责:
- file_index: 文件级元信息(路径, 语言, hash, module)
- symbol_index: 符号级信息(类/方法/函数签名, 行号, 类型)
- dependency_graph: import 依赖关系(A → B)
- 查询: 根据关键词/模块/类名精确定位符号
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# PG DDL — 结构索引相关表
# ──────────────────────────────────────────────

FILE_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS kb_file_index (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    file_path       TEXT        NOT NULL,
    language        TEXT,
    file_hash       TEXT,
    module_name     TEXT,
    last_modified   TIMESTAMPTZ DEFAULT now(),
    metadata_json   JSONB       DEFAULT '{}',
    UNIQUE(project_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_file_project  ON kb_file_index(project_id);
CREATE INDEX IF NOT EXISTS idx_file_module   ON kb_file_index(project_id, module_name);
"""

SYMBOL_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS kb_symbol_index (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    file_path       TEXT        NOT NULL,
    symbol_name     TEXT        NOT NULL,
    symbol_type     TEXT        NOT NULL,   -- class / method / function / variable / import
    start_line      INT,
    end_line        INT,
    signature       TEXT,
    docstring       TEXT,
    class_name      TEXT,                    -- 方法所属类(仅 method 类型)
    metadata_json   JSONB       DEFAULT '{}',
    UNIQUE(project_id, file_path, symbol_name, symbol_type)
);

CREATE INDEX IF NOT EXISTS idx_symbol_project     ON kb_symbol_index(project_id);
CREATE INDEX IF NOT EXISTS idx_symbol_name        ON kb_symbol_index(project_id, symbol_name);
CREATE INDEX IF NOT EXISTS idx_symbol_type        ON kb_symbol_index(project_id, symbol_type);
CREATE INDEX IF NOT EXISTS idx_symbol_class       ON kb_symbol_index(project_id, class_name);
"""

DEPENDENCY_GRAPH_DDL = """
CREATE TABLE IF NOT EXISTS kb_dependency_graph (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    source_file     TEXT        NOT NULL,
    target_file     TEXT        NOT NULL,
    import_type     TEXT        DEFAULT 'import',  -- import / from_import / dynamic
    metadata_json   JSONB       DEFAULT '{}',
    UNIQUE(project_id, source_file, target_file)
);

CREATE INDEX IF NOT EXISTS idx_dep_source ON kb_dependency_graph(project_id, source_file);
CREATE INDEX IF NOT EXISTS idx_dep_target ON kb_dependency_graph(project_id, target_file);
"""


@dataclass
class SymbolInfo:
    """单个符号的信息"""
    name: str
    symbol_type: str          # class / method / function / variable
    file_path: str
    start_line: int | None = None
    end_line: int | None = None
    signature: str | None = None
    docstring: str | None = None
    class_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileInfo:
    """文件级元信息"""
    file_path: str
    language: str | None = None
    file_hash: str | None = None
    module_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DependencyEdge:
    """依赖边"""
    source_file: str
    target_file: str
    import_type: str = "import"
    metadata: dict[str, Any] = field(default_factory=dict)


class StructureIndexer:
    """Layer A — 结构索引管理器

    提供符号表(file_index + symbol_index)与依赖图的读写操作。
    数据存储于 PostgreSQL。
    """

    ALL_DDL = [FILE_INDEX_DDL, SYMBOL_INDEX_DDL, DEPENDENCY_GRAPH_DDL]

    def __init__(self, db_config: DatabaseConfig | None = None) -> None:
        self._db_config = db_config or DatabaseConfig()
        self._conn: psycopg.AsyncConnection | None = None

    # ── 连接管理 ──────────────────────────────

    async def connect(self) -> None:
        """建立 PG 异步连接并建表"""
        self._conn = await psycopg.AsyncConnection.connect(
            self._db_config.postgres_uri, autocommit=True
        )
        await self.ensure_tables()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def ensure_tables(self) -> None:
        """创建所有结构索引表"""
        assert self._conn
        async with self._conn.cursor() as cur:
            for ddl in self.ALL_DDL:
                await cur.execute(ddl)
        logger.info("StructureIndexer tables ensured")

    def _conn_or_raise(self) -> psycopg.AsyncConnection:
        if self._conn is None:
            raise RuntimeError("StructureIndexer not connected — call connect() first")
        return self._conn

    # ── 写入: 文件索引 ─────────────────────────

    async def upsert_file(self, project_id: str, info: FileInfo) -> None:
        """插入/更新文件索引"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO kb_file_index (project_id, file_path, language, file_hash, module_name, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, file_path) DO UPDATE SET
                    language     = EXCLUDED.language,
                    file_hash    = EXCLUDED.file_hash,
                    module_name  = EXCLUDED.module_name,
                    metadata_json = EXCLUDED.metadata_json,
                    last_modified = now()
                """,
                (project_id, info.file_path, info.language, info.file_hash,
                 info.module_name, psycopg.types.json.Jsonb(info.metadata)),
            )

    async def upsert_files_batch(self, project_id: str, files: list[FileInfo]) -> None:
        """批量写入文件索引"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO kb_file_index (project_id, file_path, language, file_hash, module_name, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, file_path) DO UPDATE SET
                    language     = EXCLUDED.language,
                    file_hash    = EXCLUDED.file_hash,
                    module_name  = EXCLUDED.module_name,
                    metadata_json = EXCLUDED.metadata_json,
                    last_modified = now()
                """,
                [
                    (project_id, f.file_path, f.language, f.file_hash,
                     f.module_name, psycopg.types.json.Jsonb(f.metadata))
                    for f in files
                ],
            )

    # ── 写入: 符号索引 ─────────────────────────

    async def upsert_symbol(self, project_id: str, sym: SymbolInfo) -> None:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO kb_symbol_index
                    (project_id, file_path, symbol_name, symbol_type,
                     start_line, end_line, signature, docstring, class_name, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, file_path, symbol_name, symbol_type) DO UPDATE SET
                    start_line    = EXCLUDED.start_line,
                    end_line      = EXCLUDED.end_line,
                    signature     = EXCLUDED.signature,
                    docstring     = EXCLUDED.docstring,
                    class_name    = EXCLUDED.class_name,
                    metadata_json = EXCLUDED.metadata_json
                """,
                (project_id, sym.file_path, sym.name, sym.symbol_type,
                 sym.start_line, sym.end_line, sym.signature, sym.docstring,
                 sym.class_name, psycopg.types.json.Jsonb(sym.metadata)),
            )

    async def upsert_symbols_batch(self, project_id: str, symbols: list[SymbolInfo]) -> None:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO kb_symbol_index
                    (project_id, file_path, symbol_name, symbol_type,
                     start_line, end_line, signature, docstring, class_name, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, file_path, symbol_name, symbol_type) DO UPDATE SET
                    start_line    = EXCLUDED.start_line,
                    end_line      = EXCLUDED.end_line,
                    signature     = EXCLUDED.signature,
                    docstring     = EXCLUDED.docstring,
                    class_name    = EXCLUDED.class_name,
                    metadata_json = EXCLUDED.metadata_json
                """,
                [
                    (project_id, s.file_path, s.name, s.symbol_type,
                     s.start_line, s.end_line, s.signature, s.docstring,
                     s.class_name, psycopg.types.json.Jsonb(s.metadata))
                    for s in symbols
                ],
            )

    # ── 写入: 依赖图 ───────────────────────────

    async def upsert_dependency(self, project_id: str, edge: DependencyEdge) -> None:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO kb_dependency_graph (project_id, source_file, target_file, import_type, metadata_json)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (project_id, source_file, target_file) DO UPDATE SET
                    import_type   = EXCLUDED.import_type,
                    metadata_json = EXCLUDED.metadata_json
                """,
                (project_id, edge.source_file, edge.target_file,
                 edge.import_type, psycopg.types.json.Jsonb(edge.metadata)),
            )

    async def upsert_dependencies_batch(
        self, project_id: str, edges: list[DependencyEdge]
    ) -> None:
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO kb_dependency_graph (project_id, source_file, target_file, import_type, metadata_json)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (project_id, source_file, target_file) DO UPDATE SET
                    import_type   = EXCLUDED.import_type,
                    metadata_json = EXCLUDED.metadata_json
                """,
                [
                    (project_id, e.source_file, e.target_file,
                     e.import_type, psycopg.types.json.Jsonb(e.metadata))
                    for e in edges
                ],
            )

    # ── 查询: 精确符号检索 ─────────────────────

    async def query_symbols_by_name(
        self, project_id: str, name: str, symbol_type: str | None = None
    ) -> list[dict[str, Any]]:
        """按名称查询符号(支持类型过滤)"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            if symbol_type:
                await cur.execute(
                    """
                    SELECT file_path, symbol_name, symbol_type, start_line, end_line,
                           signature, docstring, class_name, metadata_json
                    FROM kb_symbol_index
                    WHERE project_id = %s AND symbol_name ILIKE %s AND symbol_type = %s
                    ORDER BY file_path, start_line
                    """,
                    (project_id, f"%{name}%", symbol_type),
                )
            else:
                await cur.execute(
                    """
                    SELECT file_path, symbol_name, symbol_type, start_line, end_line,
                           signature, docstring, class_name, metadata_json
                    FROM kb_symbol_index
                    WHERE project_id = %s AND symbol_name ILIKE %s
                    ORDER BY file_path, start_line
                    """,
                    (project_id, f"%{name}%"),
                )
            rows = await cur.fetchall()
        return [self._row_to_symbol_dict(r) for r in rows]

    async def query_symbols_by_class(
        self, project_id: str, class_name: str
    ) -> list[dict[str, Any]]:
        """查询属于某个类的所有方法"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT file_path, symbol_name, symbol_type, start_line, end_line,
                       signature, docstring, class_name, metadata_json
                FROM kb_symbol_index
                WHERE project_id = %s AND class_name ILIKE %s
                ORDER BY start_line
                """,
                (project_id, f"%{class_name}%"),
            )
            rows = await cur.fetchall()
        return [self._row_to_symbol_dict(r) for r in rows]

    async def query_symbols_by_file(
        self, project_id: str, file_path: str
    ) -> list[dict[str, Any]]:
        """查询某文件的所有符号"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT file_path, symbol_name, symbol_type, start_line, end_line,
                       signature, docstring, class_name, metadata_json
                FROM kb_symbol_index
                WHERE project_id = %s AND file_path = %s
                ORDER BY start_line
                """,
                (project_id, file_path),
            )
            rows = await cur.fetchall()
        return [self._row_to_symbol_dict(r) for r in rows]

    async def query_symbols_by_file_keyword(
        self, project_id: str, keyword: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        """按关键词模糊匹配【文件路径】，返回这些文件的符号。

        补 Layer A 盲区：关键词常是模块/文件名（如 'parser'、'cli'），它不是
        任何符号名，但精确指向 src/dotenv/parser.py。按符号名查会全空，按文件
        路径模糊查才能命中。
        """
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT file_path, symbol_name, symbol_type, start_line, end_line,
                       signature, docstring, class_name, metadata_json
                FROM kb_symbol_index
                WHERE project_id = %s AND file_path ILIKE %s
                ORDER BY file_path, start_line
                LIMIT %s
                """,
                (project_id, f"%{keyword}%", limit),
            )
            rows = await cur.fetchall()
        return [self._row_to_symbol_dict(r) for r in rows]

    async def query_dependencies(
        self, project_id: str, file_path: str, direction: str = "outgoing"
    ) -> list[dict[str, Any]]:
        """查询依赖关系(outgoing: 文件依赖谁; incoming: 谁依赖此文件)"""
        conn = self._conn_or_raise()
        column = "source_file" if direction == "incoming" else "target_file"
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL(
                    """
                    SELECT source_file, target_file, import_type, metadata_json
                    FROM kb_dependency_graph
                    WHERE project_id = %s AND {col} = %s
                    """
                ).format(col=sql.Identifier(column)),
                (project_id, file_path),
            )
            rows = await cur.fetchall()
        return [
            {"source": r[0], "target": r[1], "import_type": r[2], "metadata": r[3]}
            for r in rows
        ]

    async def query_transitive_deps(
        self, project_id: str, file_path: str, max_depth: int = 3
    ) -> list[str]:
        """查询传递依赖(BFS)"""
        conn = self._conn_or_raise()
        visited: set[str] = set()
        current_level = {file_path}
        result: list[str] = []

        for _ in range(max_depth):
            next_level: set[str] = set()
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT target_file FROM kb_dependency_graph
                    WHERE project_id = %s AND source_file = ANY(%s)
                    """,
                    (project_id, list(current_level)),
                )
                rows = await cur.fetchall()
            for r in rows:
                dep = r[0]
                if dep not in visited:
                    visited.add(dep)
                    result.append(dep)
                    next_level.add(dep)
            current_level = next_level
            if not current_level:
                break
        return result

    # ── 删除 ────────────────────────────────────

    async def delete_file(self, project_id: str, file_path: str) -> None:
        """删除文件及相关符号和依赖"""
        conn = self._conn_or_raise()
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM kb_symbol_index WHERE project_id = %s AND file_path = %s",
                (project_id, file_path),
            )
            await cur.execute(
                """
                DELETE FROM kb_dependency_graph
                WHERE project_id = %s AND (source_file = %s OR target_file = %s)
                """,
                (project_id, file_path, file_path),
            )
            await cur.execute(
                "DELETE FROM kb_file_index WHERE project_id = %s AND file_path = %s",
                (project_id, file_path),
            )

    # ── 内部工具 ────────────────────────────────

    @staticmethod
    def _row_to_symbol_dict(row: tuple) -> dict[str, Any]:
        return {
            "file_path": row[0],
            "symbol_name": row[1],
            "symbol_type": row[2],
            "start_line": row[3],
            "end_line": row[4],
            "signature": row[5],
            "docstring": row[6],
            "class_name": row[7],
            "metadata": row[8],
        }
