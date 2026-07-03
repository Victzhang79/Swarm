"""CodeGraph CLI 封装 — init / index / 查询

支持 @colbymchenry/codegraph（https://github.com/colbymchenry/codegraph）:
  - 数据库: .codegraph/codegraph.db
  - 表: nodes, edges, files

兼容旧版 nicolo-ribaudo/codegraph:
  - 数据库: .codegraph/data.db
  - 表: symbols/references 或 nodes/edges
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# codegraph CLI 二进制路径缓存（解析一次）
_CODEGRAPH_BIN: str | None = None


def _resolve_codegraph_bin() -> str | None:
    """定位 codegraph 可执行文件，绝对路径优先。

    API 服务进程的 PATH 可能不含 ~/.local/bin（GUI/launchd 启动时尤其常见），
    导致 shutil.which 找不到。这里显式探测常见安装位置，确保后台进程也能用。
    """
    global _CODEGRAPH_BIN
    if _CODEGRAPH_BIN and Path(_CODEGRAPH_BIN).is_file():
        return _CODEGRAPH_BIN

    # 1) 环境变量覆盖优先
    env_bin = os.environ.get("SWARM_CODEGRAPH_BIN")
    if env_bin and Path(env_bin).is_file():
        _CODEGRAPH_BIN = env_bin
        return env_bin

    # 2) PATH 中查找
    found = shutil.which("codegraph")
    if found:
        _CODEGRAPH_BIN = found
        return found

    # 3) 显式探测常见安装位置（pip --user / cargo / homebrew）
    home = Path.home()
    for c in (
        home / ".local/bin/codegraph",
        home / ".cargo/bin/codegraph",
        Path("/usr/local/bin/codegraph"),
        Path("/opt/homebrew/bin/codegraph"),
        home / "bin/codegraph",
    ):
        if c.is_file() and os.access(c, os.X_OK):
            _CODEGRAPH_BIN = str(c)
            logger.info("codegraph 二进制定位: %s", _CODEGRAPH_BIN)
            return _CODEGRAPH_BIN
    return None

# colbymchenry codegraph 中视为「代码符号」的 node kind
_SYMBOL_NODE_KINDS = frozenset({
    "function", "method", "class", "interface", "struct", "enum",
    "type", "variable", "constant", "property", "module", "namespace",
    "trait", "macro", "constructor", "destructor",
})

# 文件级依赖边（排除 contains 等结构边）
_DEPENDENCY_EDGE_KINDS = frozenset({
    "imports", "import", "calls", "call", "extends", "implements",
    "uses", "references", "depends_on", "type_ref",
})


@dataclass
class CodegraphSymbol:
    """从 codegraph DB 解析出的符号"""
    name: str
    symbol_type: str
    file_path: str
    start_line: int | None = None
    end_line: int | None = None
    signature: str | None = None
    docstring: str | None = None
    class_name: str | None = None
    qualified_name: str | None = None


@dataclass
class CodegraphEdge:
    """依赖边"""
    source_file: str
    target_file: str
    import_type: str = "import"


@dataclass
class CodegraphResult:
    """索引结果。

    P1-21：ok/error 区分【成功但空项目】(ok=True, 0 符号) 与【索引失败/部分】(ok=False)。
    先前失败分支返回裸 CodegraphResult()(0 符号无标记)，与真空项目不可分 → 上游误标 INDEXED。
    """
    symbols: list[CodegraphSymbol] = field(default_factory=list)
    edges: list[CodegraphEdge] = field(default_factory=list)
    symbol_count: int = 0
    edge_count: int = 0
    time_ms: int = 0
    db_path: str | None = None
    ok: bool = True
    error: str | None = None


def is_codegraph_installed() -> bool:
    """检查 codegraph CLI 是否已安装（PATH 健壮，绝对路径兜底）"""
    bin_path = _resolve_codegraph_bin()
    if not bin_path:
        logger.info("codegraph CLI not found in PATH or common locations")
        return False
    try:
        result = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        installed = result.returncode == 0
        if installed:
            logger.info("codegraph CLI found: %s (%s)", result.stdout.strip(), bin_path)
        else:
            logger.warning("codegraph CLI returned non-zero exit code")
        return installed
    except FileNotFoundError:
        logger.info("codegraph CLI not found at %s", bin_path)
        return False
    except subprocess.TimeoutExpired:
        logger.warning("codegraph --version timed out")
        return False
    except Exception as exc:
        logger.warning("codegraph CLI check failed: %s", exc)
        return False


def find_codegraph_db(project_path: str) -> Path | None:
    """定位 codegraph 数据库文件（优先 colbymchenry 的 codegraph.db）"""
    base = Path(project_path) / ".codegraph"
    for name in ("codegraph.db", "data.db"):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def run_codegraph_init(project_path: str) -> subprocess.CompletedProcess[str]:
    """执行 codegraph init -i，在项目目录生成 .codegraph/ 并索引"""
    cg = _resolve_codegraph_bin() or "codegraph"
    logger.info("Running codegraph init -i in %s (bin=%s)", project_path, cg)
    return subprocess.run(
        [cg, "init", "-i"],
        cwd=project_path,
        capture_output=True,
        text=True,
        timeout=600,
    )


def run_codegraph_index(project_path: str) -> subprocess.CompletedProcess[str]:
    """执行 codegraph index / sync，刷新已有索引"""
    cg = _resolve_codegraph_bin() or "codegraph"
    logger.info("Running codegraph index in %s (bin=%s)", project_path, cg)
    # colbymchenry 支持 index；若已有索引则 refresh
    result = subprocess.run(
        [cg, "index"],
        cwd=project_path,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        logger.info("codegraph index failed, trying sync: %s", result.stderr[:200])
        return subprocess.run(
            [cg, "sync"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=600,
        )
    return result


def run_codegraph_full(project_path: str) -> CodegraphResult:
    """执行 codegraph 索引流程，返回解析结果"""
    if not is_codegraph_installed():
        logger.info("codegraph not installed, skipping index")
        return CodegraphResult()

    import time as _time
    t0 = _time.monotonic()

    cg_dir = Path(project_path) / ".codegraph"
    if not cg_dir.is_dir():
        init_result = run_codegraph_init(project_path)
        if init_result.returncode != 0:
            logger.error("codegraph init failed: %s", init_result.stderr)
            return CodegraphResult(ok=False, error=f"init failed: {init_result.stderr[:500]}")
    else:
        index_result = run_codegraph_index(project_path)
        if index_result.returncode != 0:
            logger.error("codegraph index/sync failed: %s", index_result.stderr)
            return CodegraphResult(
                ok=False, error=f"index/sync failed: {index_result.stderr[:500]}"
            )

    elapsed_ms = int((_time.monotonic() - t0) * 1000)

    db_path = find_codegraph_db(project_path)
    if db_path is None:
        logger.warning("codegraph database not found under %s", cg_dir)
        return CodegraphResult(
            time_ms=elapsed_ms, ok=False, error="codegraph database not found after indexing"
        )

    result = parse_codegraph_db(str(db_path))
    result.time_ms = elapsed_ms
    result.db_path = str(db_path)
    return result


def parse_codegraph_db(db_path: str) -> CodegraphResult:
    """解析 codegraph SQLite 数据库"""
    symbols: list[CodegraphSymbol] = []
    edges: list[CodegraphEdge] = []
    parse_error: str | None = None

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row["name"] for row in cur.fetchall()}

        if "nodes" in tables:
            symbols = _parse_colbymchenry_nodes(cur)
            if "edges" in tables:
                edges = _parse_colbymchenry_edges(cur)
        elif "symbols" in tables:
            symbols = _parse_symbols_table(cur)
            if "references" in tables:
                edges = _parse_references_table(cur)

        conn.close()
    except sqlite3.Error as exc:
        # P1-21：解析失败 → ok=False，避免部分/空结果被上游误标 INDEXED。
        logger.error("Failed to parse codegraph db %s: %s", db_path, exc)
        parse_error = f"db parse failed: {exc}"

    return CodegraphResult(
        symbols=symbols,
        edges=edges,
        symbol_count=len(symbols),
        edge_count=len(edges),
        db_path=db_path,
        ok=parse_error is None,
        error=parse_error,
    )


def _parse_colbymchenry_nodes(cur: sqlite3.Cursor) -> list[CodegraphSymbol]:
    """解析 colbymchenry/codegraph 的 nodes 表"""
    symbols: list[CodegraphSymbol] = []
    try:
        cur.execute("SELECT * FROM nodes")
        cols = {desc[0] for desc in cur.description}
        for row in cur.fetchall():
            kind = _col(row, cols, "kind", "function")
            if kind in ("file", "import", "directory"):
                continue
            if kind not in _SYMBOL_NODE_KINDS and kind not in ("function", "class", "method"):
                # 保留未知 kind 中看起来像符号的行
                if kind in ("module",):
                    pass
                elif kind not in _SYMBOL_NODE_KINDS:
                    continue

            qname = _col(row, cols, "qualified_name", "")
            class_name = _extract_class_from_qname(qname)

            sym = CodegraphSymbol(
                name=_col(row, cols, "name", ""),
                symbol_type=kind,
                file_path=_col(row, cols, "file_path", ""),
                start_line=_col(row, cols, "start_line", None),
                end_line=_col(row, cols, "end_line", None),
                signature=_col(row, cols, "signature", None),
                docstring=_col(row, cols, "docstring", None),
                class_name=class_name,
                qualified_name=qname or None,
            )
            if sym.name and sym.file_path:
                symbols.append(sym)
    except sqlite3.Error as exc:
        logger.warning("Failed to parse nodes table: %s", exc)
    return symbols


def _parse_colbymchenry_edges(cur: sqlite3.Cursor) -> list[CodegraphEdge]:
    """解析 colbymchenry/codegraph 的 edges 表（join nodes 得到文件路径）"""
    edges: list[CodegraphEdge] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        cur.execute(
            """
            SELECT DISTINCT
                n1.file_path AS source_file,
                n2.file_path AS target_file,
                e.kind AS edge_kind
            FROM edges e
            JOIN nodes n1 ON e.source = n1.id
            JOIN nodes n2 ON e.target = n2.id
            WHERE n1.file_path != n2.file_path
            """
        )
        cols = {desc[0] for desc in cur.description}
        for row in cur.fetchall():
            edge_kind = _col(row, cols, "edge_kind", _col(row, cols, "kind", "import"))
            if edge_kind == "contains":
                continue
            if edge_kind not in _DEPENDENCY_EDGE_KINDS and edge_kind not in ("imports", "calls"):
                # 保留常见跨文件关系
                if edge_kind not in ("references", "uses"):
                    continue

            src = _col(row, cols, "source_file", "")
            tgt = _col(row, cols, "target_file", "")
            if not src or not tgt:
                continue
            key = (src, tgt, edge_kind)
            if key in seen:
                continue
            seen.add(key)
            edges.append(CodegraphEdge(source_file=src, target_file=tgt, import_type=edge_kind))
    except sqlite3.Error as exc:
        logger.warning("Failed to parse edges with join: %s", exc)
    return edges


def _extract_class_from_qname(qualified_name: str) -> str | None:
    if not qualified_name or "." not in qualified_name:
        return None
    parts = qualified_name.rsplit(".", 1)
    if len(parts) == 2 and parts[0]:
        return parts[0].split(".")[-1]
    return None


def _parse_symbols_table(cur: sqlite3.Cursor) -> list[CodegraphSymbol]:
    symbols: list[CodegraphSymbol] = []
    try:
        cur.execute("SELECT * FROM symbols")
        cols = {desc[0] for desc in cur.description}
        for row in cur.fetchall():
            sym = CodegraphSymbol(
                name=_col(row, cols, "name", ""),
                symbol_type=_col(row, cols, "kind", _col(row, cols, "type", "function")),
                file_path=_col(row, cols, "file_path", _col(row, cols, "file", "")),
                start_line=_col(row, cols, "start_line", _col(row, cols, "line", None)),
                end_line=_col(row, cols, "end_line", None),
                signature=_col(row, cols, "signature", None),
                docstring=_col(row, cols, "docstring", _col(row, cols, "docs", None)),
                class_name=_col(row, cols, "class_name", _col(row, cols, "parent", None)),
            )
            symbols.append(sym)
    except sqlite3.Error as exc:
        logger.warning("Failed to parse symbols table: %s", exc)
    return symbols


def _parse_references_table(cur: sqlite3.Cursor) -> list[CodegraphEdge]:
    edges: list[CodegraphEdge] = []
    try:
        # references 是 SQL 保留字，必须加引号(P2)。SQLite 宽松放行，但裸名在严格方言
        # (PostgreSQL 等)会语法报错；加双引号既兼容当前 SQLite 又防未来迁移踩雷。
        cur.execute('SELECT * FROM "references"')
        cols = {desc[0] for desc in cur.description}
        for row in cur.fetchall():
            edge = CodegraphEdge(
                source_file=_col(row, cols, "source_file", _col(row, cols, "from_file", "")),
                target_file=_col(row, cols, "target_file", _col(row, cols, "to_file", "")),
                import_type=_col(row, cols, "import_type", _col(row, cols, "type", "import")),
            )
            edges.append(edge)
    except sqlite3.Error as exc:
        logger.warning("Failed to parse references table: %s", exc)
    return edges


def _col(row: sqlite3.Row, cols: set[str], name: str, default: Any) -> Any:
    if name not in cols:
        return default
    val = row[name]
    return val if val is not None else default
