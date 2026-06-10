"""增量更新系统 — Git push webhook → 事件队列 → 各层更新

负责:
- 接收 Git 变更事件(commit / push)
- 计算文件级 diff(哪些文件增删改)
- 分发到各知识层更新:
  - Layer A: 重新索引变更文件的结构信息
  - Layer B: 重新索引变更文件的语义 chunk
  - Layer C: 规范不变(人工管理)
  - Layer D: 记录修改日志 + 更新共现
- 支持异步事件队列(Redis stream)或直接调用
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

import psycopg

from swarm.config.settings import DatabaseConfig, KnowledgeConfig
from swarm.knowledge.behavior_store import BehaviorStore, ModificationRecord
from swarm.knowledge.semantic_index import SemanticIndexer
from swarm.knowledge.structure_index import (
    FileInfo,
    StructureIndexer,
    SymbolInfo,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 事件定义
# ──────────────────────────────────────────────

class ChangeType(str, Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass
class FileChange:
    """单个文件变更"""
    file_path: str
    change_type: ChangeType
    old_path: str | None = None          # RENAMED 时的原始路径
    content: str | None = None            # 新内容(MODIFIED/ADDED)
    diff: str | None = None               # git diff
    language: str | None = None


@dataclass
class UpdateEvent:
    """知识库更新事件"""
    project_id: str
    task_id: str | None = None
    commit_hash: str | None = None
    author: str | None = None
    changes: list[FileChange] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def dedupe_changes(changes: list[FileChange]) -> list[FileChange]:
    """同路径保留最后一次变更（批内/跨 commit 去重）。"""
    if not changes:
        return []
    by_path: dict[str, FileChange] = {}
    order: list[str] = []
    for change in changes:
        key = change.file_path.replace("\\", "/")
        if key not in by_path:
            order.append(key)
        by_path[key] = replace(change, file_path=key)
    return [by_path[k] for k in order]


def dedupe_event(event: UpdateEvent) -> UpdateEvent:
    """事件级去重并写入 metadata。"""
    deduped = dedupe_changes(event.changes)
    if len(deduped) != len(event.changes):
        meta = dict(event.metadata or {})
        meta["deduped_from"] = len(event.changes)
        return replace(event, changes=deduped, metadata=meta)
    return event


def _merge_project_events(events: list[UpdateEvent], project_id: str) -> UpdateEvent:
    """将同一 project_id 的多个事件合并为一个，文件变更去重。

    用于 process_pending_events 实现 ~5s 窗口批量合并:
    同一项目 5 秒内累积的多个事件合并后一次性处理，减少重复索引。
    同一文件只保留最后一次变更状态（ADDED 后又 DELETED 则以最后为准）。
    """
    # 按事件原始顺序收集所有变更
    all_changes: list[FileChange] = []
    # 合并 metadata，后者覆盖前者
    merged_meta: dict[str, Any] = {}
    # 取最后一个非 None 值的字段
    task_id = None
    commit_hash = None
    author = None
    for ev in events:
        all_changes.extend(ev.changes)
        if ev.metadata:
            merged_meta.update(ev.metadata)
        if ev.task_id is not None:
            task_id = ev.task_id
        if ev.commit_hash is not None:
            commit_hash = ev.commit_hash
        if ev.author is not None:
            author = ev.author

    # 复用 dedupe_changes 按路径去重，同路径保留最后一次
    deduped = dedupe_changes(all_changes)

    # 记录合并来源信息
    if len(events) > 1:
        merged_meta["batch_merged_from"] = len(events)
        merged_meta["batch_changes_before_dedup"] = len(all_changes)

    return UpdateEvent(
        project_id=project_id,
        task_id=task_id,
        commit_hash=commit_hash,
        author=author,
        changes=deduped,
        metadata=merged_meta,
    )


def _language_from_path(file_path: str) -> str | None:
    ext = Path(file_path).suffix.lower()
    return {
        ".py": "python",
        ".java": "java",
        ".kt": "kotlin",
        ".go": "go",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
    }.get(ext)


def hydrate_event_changes(event: UpdateEvent) -> UpdateEvent:
    """从 project_path 补全 ADDED/MODIFIED 的 content（队列回放用）。"""
    project_path = (event.metadata or {}).get("project_path")
    if not project_path:
        return event
    root = Path(project_path)
    hydrated: list[FileChange] = []
    for change in event.changes:
        if change.change_type == ChangeType.DELETED:
            hydrated.append(change)
            continue
        if change.content:
            lang = change.language or _language_from_path(change.file_path)
            hydrated.append(replace(change, language=lang) if lang and not change.language else change)
            continue
        full = root / change.file_path
        if not full.is_file():
            if change.change_type in (ChangeType.ADDED, ChangeType.MODIFIED):
                hydrated.append(
                    replace(change, change_type=ChangeType.DELETED)
                )
            else:
                hydrated.append(change)
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lang = change.language or _language_from_path(change.file_path)
        hydrated.append(replace(change, content=content, language=lang))
    return replace(event, changes=hydrated)


# ──────────────────────────────────────────────
# 事件队列表(SQL)
# ──────────────────────────────────────────────

EVENT_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS kb_update_events (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    event_type      TEXT        DEFAULT 'push',
    payload_json    JSONB       NOT NULL,
    status          TEXT        DEFAULT 'pending',    -- pending / processing / done / failed
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_event_status   ON kb_update_events(status);
CREATE INDEX IF NOT EXISTS idx_event_project  ON kb_update_events(project_id);

-- Layer B embedding 重试队列：embedding 服务不可用时暂存，恢复后补处理
CREATE TABLE IF NOT EXISTS kb_pending_embeddings (
    project_id    TEXT        NOT NULL,
    file_path     TEXT        NOT NULL,
    change_type   TEXT        DEFAULT 'modified',
    language      TEXT,
    retry_count   INT         DEFAULT 0,
    last_error    TEXT,
    created_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (project_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_pending_embed_project ON kb_pending_embeddings(project_id);
"""


class KnowledgeUpdater:
    """增量更新系统

    处理流程:
    1. 接收 UpdateEvent
    2. 对每个 FileChange:
       - ADDED/MODIFIED: 重新提取结构 + 语义索引
       - DELETED: 删除结构 + 语义索引
       - RENAMED: 删除旧 + 索引新
    3. 记录修改日志(Layer D)
    4. 更新共现关系
    """

    def __init__(
        self,
        db_config: DatabaseConfig | None = None,
        kb_config: KnowledgeConfig | None = None,
    ) -> None:
        self._db_config = db_config or DatabaseConfig()
        self._kb_config = kb_config or KnowledgeConfig()

        self._struct: StructureIndexer | None = None
        self._semantic: SemanticIndexer | None = None
        self._behavior: BehaviorStore | None = None
        self._conn: psycopg.AsyncConnection | None = None
        # 单例 updater 的连接（含子索引器）被 HTTP webhook 入队与后台轮询消费
        # 并发共享；psycopg AsyncConnection 不支持并发查询，用锁串行化临界区。
        self._lock = asyncio.Lock()

    # ── 连接管理 ──────────────────────────────

    async def connect(self) -> None:
        """连接所有组件"""
        self._conn = await psycopg.AsyncConnection.connect(
            self._db_config.postgres_uri, autocommit=True
        )
        async with self._conn.cursor() as cur:
            await cur.execute(EVENT_QUEUE_DDL)

        self._struct = StructureIndexer(self._db_config)
        await self._struct.connect()

        self._semantic = SemanticIndexer(self._db_config, self._kb_config)
        await self._semantic.connect()

        self._behavior = BehaviorStore(self._db_config)
        await self._behavior.connect()

        logger.info("KnowledgeUpdater connected")

    async def close(self) -> None:
        if self._struct:
            await self._struct.close()
        if self._semantic:
            await self._semantic.close()
        if self._behavior:
            await self._behavior.close()
        if self._conn:
            await self._conn.close()

    # ── 注入外部组件 ──────────────────────────

    def set_structure_indexer(self, indexer: StructureIndexer) -> None:
        self._struct = indexer

    def set_semantic_indexer(self, indexer: SemanticIndexer) -> None:
        self._semantic = indexer

    def set_behavior_store(self, store: BehaviorStore) -> None:
        self._behavior = store

    # ── 主入口: 处理事件 ──────────────────────

    async def handle_event(self, event: UpdateEvent) -> dict[str, Any]:
        """处理一个更新事件"""
        event = dedupe_event(event)
        event = hydrate_event_changes(event)
        result: dict[str, Any] = {
            "project_id": event.project_id,
            "total_changes": len(event.changes),
            "layers_updated": [],
            "errors": [],
        }

        for change in event.changes:
            try:
                await self._process_change(event.project_id, change)
            except Exception as e:
                logger.exception("Error processing change %s", change.file_path)
                result["errors"].append({
                    "file": change.file_path,
                    "error": str(e),
                })

        # Layer D: 记录修改日志 + 共现更新
        try:
            await self._update_layer_d(event)
            result["layers_updated"].append("D")
        except Exception as e:
            logger.exception("Error updating Layer D")
            result["errors"].append({"layer": "D", "error": str(e)})

        logger.info(
            "handle_event: project=%s changes=%d updated=%s errors=%d",
            event.project_id,
            len(event.changes),
            result["layers_updated"],
            len(result["errors"]),
        )
        return result

    async def _process_change(
        self, project_id: str, change: FileChange
    ) -> None:
        """处理单个文件变更"""
        if change.change_type == ChangeType.DELETED:
            # Layer A: 删除结构索引
            if self._struct:
                await self._struct.delete_file(project_id, change.file_path)
            # Layer B: 删除语义索引
            if self._semantic:
                await self._semantic.delete_by_file(project_id, change.file_path)

        elif change.change_type == ChangeType.RENAMED:
            if change.old_path:
                # 删除旧
                if self._struct:
                    await self._struct.delete_file(project_id, change.old_path)
                if self._semantic:
                    await self._semantic.delete_by_file(project_id, change.old_path)
            # 索引新
            if change.content:
                await self._index_file(project_id, change)

        else:  # ADDED / MODIFIED
            if change.content:
                await self._index_file(project_id, change)

    async def _index_file(
        self, project_id: str, change: FileChange
    ) -> None:
        """索引一个文件到 Layer A + Layer B"""
        # Layer A: 提取结构信息并写入
        if self._struct:
            file_info = FileInfo(
                file_path=change.file_path,
                language=change.language,
                file_hash=_simple_hash(change.content or ""),
                module_name=_guess_module(change.file_path),
            )
            await self._struct.upsert_file(project_id, file_info)

            # 提取符号(Python 用 ast 精确解析，其他语言用正则兜底)
            symbols = _extract_symbols_simple(
                change.content, change.file_path, change.language
            )
            await self._struct.upsert_symbols_batch(project_id, symbols)

        # Layer B: 切分 + 语义索引（embedding 服务不可用时显式降级，不拖垮 Layer A）
        if self._semantic:
            try:
                # 先删除旧的 chunks
                await self._semantic.delete_by_file(project_id, change.file_path)
                # 重新索引
                await self._semantic.index_source_file(
                    project_id,
                    change.content or "",
                    change.file_path,
                    module_name=_guess_module(change.file_path),
                )
            except Exception as exc:
                # embedding 服务/Qdrant 不可用 → Layer A 已成功，将该文件的 Layer B
                # 更新暂存到重试队列，待服务恢复后由 retry_pending_embeddings 补处理。
                logger.warning(
                    "[Updater] Layer B 语义索引失败，降级暂存待重试: %s (%s)",
                    change.file_path, exc,
                )
                await self._defer_embedding_retry(project_id, change)

    async def _defer_embedding_retry(
        self, project_id: str, change: FileChange
    ) -> None:
        """embedding 失败时，将文件暂存到重试队列（Layer A 已成功）。"""
        if not self._conn:
            return
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO kb_pending_embeddings
                        (project_id, file_path, change_type, language, retry_count)
                    VALUES (%s, %s, %s, %s, 0)
                    ON CONFLICT (project_id, file_path) DO UPDATE SET
                        change_type = EXCLUDED.change_type,
                        language    = EXCLUDED.language,
                        created_at  = now()
                    """,
                    (
                        project_id,
                        change.file_path,
                        change.change_type.value,
                        change.language,
                    ),
                )
        except Exception as exc:
            logger.debug("[Updater] 暂存 embedding 重试失败: %s", exc)

    async def retry_pending_embeddings(
        self, project_id: str | None = None, *, limit: int = 50
    ) -> int:
        """重试暂存的 Layer B embedding（embedding 服务恢复后调用）。

        成功索引则从重试队列移除；仍失败则累加 retry_count。
        返回成功补处理的文件数。
        """
        if not self._conn or not self._semantic:
            return 0

        # 与轮询/入队共享连接，串行化
        async with self._lock:
            # 跳过重试已达上限的条目（retry_count >= 10 视为永久失败，避免无限空转）。
            # 这些条目保留在表中供排查，但不再自动重试。
            if project_id:
                where = "WHERE project_id = %s AND retry_count < 10"
                params: tuple = (project_id, limit)
            else:
                where = "WHERE retry_count < 10"
                params = (limit,)
            async with self._conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT project_id, file_path, language
                    FROM kb_pending_embeddings
                    {where}
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    params,
                )
                rows = await cur.fetchall()

            if not rows:
                return 0

            succeeded = 0
            project_path_cache: dict[str, str | None] = {}
            for pid, file_path, language in rows:
                # 从项目工作区读取最新文件内容
                if pid not in project_path_cache:
                    project_path_cache[pid] = _lookup_project_path(pid)
                proj_path = project_path_cache[pid]
                content = None
                if proj_path:
                    try:
                        fp = Path(proj_path) / file_path
                        if fp.is_file():
                            content = fp.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        content = None
                if content is None:
                    continue
                try:
                    await self._semantic.delete_by_file(pid, file_path)
                    await self._semantic.index_source_file(
                        pid, content, file_path,
                        module_name=_guess_module(file_path),
                    )
                    async with self._conn.cursor() as cur:
                        await cur.execute(
                            "DELETE FROM kb_pending_embeddings WHERE project_id=%s AND file_path=%s",
                            (pid, file_path),
                        )
                    succeeded += 1
                except Exception as exc:
                    async with self._conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE kb_pending_embeddings
                            SET retry_count = retry_count + 1, last_error = %s
                            WHERE project_id=%s AND file_path=%s
                            """,
                            (str(exc)[:300], pid, file_path),
                        )
        if succeeded:
            logger.info("[Updater] 补处理 %d 个暂存 embedding", succeeded)
        return succeeded

    async def _update_layer_d(self, event: UpdateEvent) -> None:
        """更新 Layer D: 记录修改日志"""
        if not self._behavior:
            return

        records = [
            ModificationRecord(
                file_path=c.file_path,
                task_id=event.task_id,
                change_type=c.change_type.value,
                commit_hash=event.commit_hash,
                author=event.author,
            )
            for c in event.changes
        ]
        await self._behavior.log_modifications_batch(event.project_id, records)

    # ── 事件队列 ────────────────────────────────

    async def enqueue_event(self, event: UpdateEvent) -> int:
        """将事件放入队列(异步处理)"""
        assert self._conn
        event = dedupe_event(event)
        payload = {
            "project_id": event.project_id,
            "task_id": event.task_id,
            "commit_hash": event.commit_hash,
            "author": event.author,
            "changes": [
                {
                    "file_path": c.file_path,
                    "change_type": c.change_type.value,
                    "old_path": c.old_path,
                    "language": c.language,
                }
                for c in event.changes
            ],
            "metadata": event.metadata,
        }
        async with self._lock:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO kb_update_events (project_id, payload_json)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (event.project_id, psycopg.types.json.Jsonb(payload)),
                )
                row = await cur.fetchone()
        return row[0]

    async def process_pending_events(self, batch_size: int = 10) -> int:
        """处理队列中的待处理事件（~5s 窗口批量合并）。

        调度器每 5 秒轮询一次，本次拉出的就是过去 ~5s 累积的事件。
        按 project_id 分组后合并去重，同一项目的多个事件合为一批处理，
        减少重复索引开销。某项目处理失败不影响其他项目。
        """
        assert self._conn
        processed = 0

        # 串行化：与 enqueue_event 共享同一 AsyncConnection，不能并发查询
        async with self._lock:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE kb_update_events SET status = 'processing'
                    WHERE id IN (
                        SELECT id FROM kb_update_events
                        WHERE status = 'pending'
                        ORDER BY created_at ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, project_id, payload_json
                    """,
                    (batch_size,),
                )
                rows = await cur.fetchall()

            if not rows:
                return 0

            # 按 project_id 分组，保持原始顺序
            groups: OrderedDict[str, list[tuple[int, UpdateEvent]]] = OrderedDict()
            for row in rows:
                event_id, project_id, payload = row
                event = _payload_to_event(project_id, payload)
                groups.setdefault(project_id, []).append((event_id, event))

            # 按项目批量合并处理
            for project_id, items in groups.items():
                event_ids = [eid for eid, _ in items]
                events = [ev for _, ev in items]
                try:
                    # 同项目多事件合并为一个，文件变更去重
                    merged = _merge_project_events(events, project_id)
                    await self.handle_event(merged)
                    # 该项目所有事件都标 done
                    async with self._conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE kb_update_events
                            SET status = 'done', processed_at = now()
                            WHERE id = ANY(%s)
                            """,
                            (event_ids,),
                        )
                    processed += len(event_ids)
                except Exception as e:
                    logger.exception(
                        "Failed to process batch for project %s (%d events)",
                        project_id, len(event_ids),
                    )
                    # 该项目所有事件都标 failed
                    async with self._conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE kb_update_events
                            SET status = 'failed', error_message = %s
                            WHERE id = ANY(%s)
                            """,
                            (str(e)[:500], event_ids),
                        )

        return processed


# ──────────────────────────────────────────────
# 内部工具函数
# ──────────────────────────────────────────────

def _simple_hash(content: str) -> str:
    """简单内容 hash"""
    import hashlib
    return hashlib.md5(content.encode()).hexdigest()[:16]


def _lookup_project_path(project_id: str) -> str | None:
    """查项目工作区路径（用于 embedding 重试时读取最新文件内容）。"""
    try:
        from swarm.project import store

        proj = store.get_project(project_id)
        if proj and proj.get("path"):
            return proj["path"]
    except Exception as exc:
        logger.debug("[Updater] 获取项目路径失败 %s: %s", project_id, exc)
    return None


def _guess_module(file_path: str) -> str | None:
    """从文件路径猜测模块名(简单启发式)"""
    parts = file_path.replace("\\", "/").split("/")
    # 排除常见前缀
    skip = {"src", "lib", "app", "pkg", "internal", "cmd", "cmd"}
    filtered = [p for p in parts[:-1] if p not in skip and not p.startswith(".")]
    if filtered:
        return ".".join(filtered)
    return None


def _extract_symbols_python_ast(source: str, file_path: str) -> list[SymbolInfo] | None:
    """用 stdlib ast 精确提取 Python 符号（类/方法/函数，含嵌套、装饰器、async）。

    比正则更健壮：正确处理多行签名、嵌套类、async def、装饰器、docstring，
    并能给出准确的 start_line / end_line。解析失败（语法错误）返回 None，
    调用方回退到正则。
    """
    import ast

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    symbols: list[SymbolInfo] = []

    def _signature(node: ast.AST) -> str:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            if node.args.vararg:
                args.append("*" + node.args.vararg.arg)
            if node.args.kwarg:
                args.append("**" + node.args.kwarg.arg)
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            return f"{prefix} {node.name}({', '.join(args)})"
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else []
            return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
        return ""

    def _visit(node: ast.AST, class_name: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                symbols.append(SymbolInfo(
                    name=child.name,
                    symbol_type="class",
                    file_path=file_path,
                    start_line=child.lineno,
                    end_line=getattr(child, "end_lineno", None),
                    signature=_signature(child),
                    docstring=ast.get_docstring(child),
                ))
                _visit(child, child.name)  # 递归进入类体，提取方法
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(SymbolInfo(
                    name=child.name,
                    symbol_type="method" if class_name else "function",
                    file_path=file_path,
                    start_line=child.lineno,
                    end_line=getattr(child, "end_lineno", None),
                    signature=_signature(child),
                    docstring=ast.get_docstring(child),
                    class_name=class_name,
                ))
                # 进入函数体提取嵌套函数；嵌套函数不再归属于外层类
                _visit(child, None)

    _visit(tree, None)
    return symbols


def _extract_symbols_simple(
    source: str, file_path: str, language: str | None = None
) -> list[SymbolInfo]:
    """提取符号 — Python 用 stdlib ast（精确），其他语言用正则（兜底）。

    支持 Python / Java / Go / TypeScript 风格的类/函数定义。
    Python 文件优先用 ast 解析；解析失败（语法错误）回退到正则。
    """
    import re

    symbols: list[SymbolInfo] = []
    lines = source.splitlines()
    lang = (language or "").lower()

    if lang in ("python", "py"):
        ast_symbols = _extract_symbols_python_ast(source, file_path)
        if ast_symbols is not None:
            return ast_symbols
        # ast 解析失败（语法错误）→ 回退到正则
        current_class: str | None = None
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            # class 定义
            m = re.match(r"^class\s+(\w+)", stripped)
            if m:
                current_class = m.group(1)
                symbols.append(SymbolInfo(
                    name=current_class,
                    symbol_type="class",
                    file_path=file_path,
                    start_line=i,
                    signature=stripped,
                ))
                continue
            # method / function 定义
            m = re.match(r"^(async\s+)?def\s+(\w+)\s*\(([^)]*)\)", stripped)
            if m:
                func_name = m.group(2)
                params = m.group(3)
                symbols.append(SymbolInfo(
                    name=func_name,
                    symbol_type="method" if current_class and line.startswith("    ") else "function",
                    file_path=file_path,
                    start_line=i,
                    signature=f"def {func_name}({params})",
                    class_name=current_class if line.startswith("    ") else None,
                ))

    elif lang in ("java", "kotlin", "kt"):
        current_class: str | None = None
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            m = re.match(r"(?:public|private|protected)?\s*(?:abstract\s+)?(?:class|interface)\s+(\w+)", stripped)
            if m:
                current_class = m.group(1)
                symbols.append(SymbolInfo(
                    name=current_class,
                    symbol_type="class",
                    file_path=file_path,
                    start_line=i,
                    signature=stripped,
                ))
                continue
            m = re.match(
                r"(?:public|private|protected)?\s*(?:static\s+)?(?:[\w<>\[\],\s]+)\s+(\w+)\s*\(([^)]*)\)",
                stripped,
            )
            if m and current_class:
                symbols.append(SymbolInfo(
                    name=m.group(1),
                    symbol_type="method",
                    file_path=file_path,
                    start_line=i,
                    signature=stripped[:200],
                    class_name=current_class,
                ))

    elif lang in ("go", "golang"):
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            m = re.match(r"func\s+(?:\([\w\s\*]+\)\s+)?(\w+)\s*\(([^)]*)\)", stripped)
            if m:
                symbols.append(SymbolInfo(
                    name=m.group(1),
                    symbol_type="function",
                    file_path=file_path,
                    start_line=i,
                    signature=stripped[:200],
                ))

    elif lang in ("typescript", "ts", "javascript", "js"):
        current_class: str | None = None
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            m = re.match(r"(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)", stripped)
            if m:
                current_class = m.group(1)
                symbols.append(SymbolInfo(
                    name=current_class,
                    symbol_type="class",
                    file_path=file_path,
                    start_line=i,
                    signature=stripped[:200],
                ))
            m = re.match(
                r"(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[a-zA-Z_]\w*)\s*=>)",
                stripped,
            )
            if m:
                func_name = m.group(1) or m.group(2)
                symbols.append(SymbolInfo(
                    name=func_name,
                    symbol_type="function",
                    file_path=file_path,
                    start_line=i,
                    signature=stripped[:200],
                ))
    else:
        # 通用: 尝试提取所有函数/方法定义
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            m = re.match(r"^(?:def|func|function|fn)\s+(\w+)", stripped)
            if m:
                symbols.append(SymbolInfo(
                    name=m.group(1),
                    symbol_type="function",
                    file_path=file_path,
                    start_line=i,
                    signature=stripped[:200],
                ))

    return symbols


def _payload_to_event(project_id: str, payload: dict[str, Any]) -> UpdateEvent:
    """从 JSON payload 构造 UpdateEvent"""
    changes: list[FileChange] = []
    for c in payload.get("changes", []):
        changes.append(FileChange(
            file_path=c["file_path"],
            change_type=ChangeType(c["change_type"]),
            old_path=c.get("old_path"),
            language=c.get("language"),
        ))
    return UpdateEvent(
        project_id=project_id,
        task_id=payload.get("task_id"),
        commit_hash=payload.get("commit_hash"),
        author=payload.get("author"),
        changes=changes,
        metadata=payload.get("metadata", {}),
    )
