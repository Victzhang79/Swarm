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
import os
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
        # #4(b) 治本：Worker 回灌事件（dispatch._feedback_to_knowledge）常缺 metadata.project_path
        # 且事件本就不带 content → 过去直接 return → content 永空 → _index_file 被跳过 → 回灌静默
        # no-op（Layer A/B 一字不索引）。从 event.project_id 兜底解析工作区路径，一处覆盖所有缺
        # project_path 的入队方（含未来新入口）。
        project_path = _lookup_project_path(event.project_id) if event.project_id else None
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

-- D39：卡死恢复所需列（幂等迁移）。claimed_at=出队认领时刻（staleness 按处理时长而非入队龄）；
-- retry_count=stale-processing 重置/failed 重试的有界计数（防毒事件无限崩溃循环）。
ALTER TABLE kb_update_events ADD COLUMN IF NOT EXISTS retry_count INT DEFAULT 0;
ALTER TABLE kb_update_events ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

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
        # 每项目增量变更计数器（进程内）；累积到阈值触发依赖图后台重建后清零。
        # 用 in-process dict（最简健壮）：重启即重置不影响正确性——增量已删自身
        # 出边兜底，重建只是纠正缺边的尽力而为优化。
        self._depgraph_dirty: dict[str, int] = {}
        # 持后台重建 task 强引用，防 GC 中途回收（与 api.app._spawn_bg 同纪律）；
        # 完成即从集合 discard。
        self._depgraph_tasks: set = set()

    # ── 连接管理 ──────────────────────────────

    async def connect(self) -> None:
        """连接所有组件"""
        if self._conn is not None:
            return  # TD2606-B16：幂等守卫——重复 connect 不再丢弃旧连接造成泄漏
        from swarm.infra.db import pg_connect_timeout_kwargs

        # D15：直连（不走池）补 connect_timeout——PG 网络黑洞时有界快失败，不无限挂。
        self._conn = await psycopg.AsyncConnection.connect(
            self._db_config.postgres_uri, autocommit=True, **pg_connect_timeout_kwargs()
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
        # TD2606-C14：取消在飞的后台 depgraph 重建任务，避免其写向即将关闭的连接（孤儿协程）。
        for _t in list(self._depgraph_tasks):
            _t.cancel()
        self._depgraph_tasks.clear()
        if self._struct:
            await self._struct.close()
        if self._semantic:
            await self._semantic.close()
        if self._behavior:
            await self._behavior.close()
        if self._conn:
            await self._conn.close()
        # 复核 storage#1 治本：置空所有连接引用。connect() 的幂等守卫是 `if self._conn is not None:
        # return`——close 后不置空则 _conn 仍指向【已关闭】连接，shutdown→close→再 connect 会复用
        # 已关连接，后续 SQL 全落死连接。置空后 connect() 会重建全部组件。
        self._conn = None
        self._struct = None
        self._semantic = None
        self._behavior = None

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
            # #4(a) per-file 对账：先删本文件旧符号再写新符号（delete-then-insert）。upsert 纯
            # INSERT ON CONFLICT 不会清掉本文件中已消失的符号 → 幽灵符号累积、检索单调劣化。
            # 与下方 delete_outgoing_dependencies 出边对账同构。失败不拖垮 Layer A。
            try:
                await self._struct.delete_symbols_by_file(project_id, change.file_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Updater] 删除旧符号失败(忽略): %s (%s)", change.file_path, exc)
            await self._struct.upsert_symbols_batch(project_id, symbols)

            # 依赖图维护(K4-a): 删除该文件的旧出边。增量不重建依赖图，旧 import
            # 关系可能已失效——删出边让 query_transitive_deps 不再服务错误边
            # （缺边在 BFS 中优雅降级）。失败不得拖垮 Layer A。
            try:
                await self._struct.delete_outgoing_dependencies(
                    project_id, change.file_path
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Updater] 删除旧出边失败(忽略): %s (%s)",
                    change.file_path, exc,
                )
            # 依赖图维护(K4-b): 累积漂移到阈值后台触发真重建。
            await self._maybe_rebuild_depgraph(project_id)

        # Layer B: 切分 + 语义索引（embedding 服务不可用时显式降级，不拖垮 Layer A）
        if self._semantic:
            try:
                # write-then-prune（替代先删后索引）：先 upsert 新 chunk 打代际，成功后删旧代际，
                # index 失败则旧 chunk 原样保留（无向量空窗），走下方降级重试。
                await self._semantic.reindex_file_atomic(
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

    async def _maybe_rebuild_depgraph(self, project_id: str) -> None:
        """累积每项目增量变更计数；到阈值后台触发依赖图重建并清零。

        全程 fail-soft：阈值<=0 关闭自动重建（仅累积日志）；重建在后台任务里跑，
        失败被吞掉，绝不影响当前文件的索引成功。
        """
        threshold = int(
            getattr(self._kb_config, "depgraph_rebuild_threshold", 50) or 0
        )
        count = self._depgraph_dirty.get(project_id, 0) + 1
        self._depgraph_dirty[project_id] = count
        if threshold <= 0:
            return
        if count < threshold:
            return
        # 到阈值：清零并后台触发重建（fire-and-forget，不阻塞索引）。
        self._depgraph_dirty[project_id] = 0
        logger.info(
            "[Updater] 项目 %s 增量变更累计 %d 次(>=%d)，后台触发依赖图重建",
            project_id, count, threshold,
        )
        try:
            task = asyncio.create_task(self._rebuild_depgraph_async(project_id))
            self._depgraph_tasks.add(task)
            task.add_done_callback(self._depgraph_tasks.discard)
        except RuntimeError:
            # 无运行中的事件循环（极少见）→ 直接 await，失败也吞掉。
            try:
                await self._rebuild_depgraph_async(project_id)
            except Exception:  # noqa: BLE001
                logger.exception("[Updater] 依赖图重建失败(忽略): %s", project_id)

    async def _rebuild_depgraph_async(self, project_id: str) -> None:
        """复用 preprocess 的 codegraph 依赖抽取路径重建该项目依赖图。

        从 project store 取项目路径 → 跑 codegraph → 删本项目旧边 → 写新边。
        懒导入避免与 preprocess/project 形成循环依赖。失败必须 fail-soft。
        """
        try:
            from swarm.project import store as _store
            from swarm.project.codegraph import (
                is_codegraph_installed,
                run_codegraph_full,
            )
            from swarm.project.preprocess import _replace_dependency_graph

            loop = asyncio.get_running_loop()
            proj = await loop.run_in_executor(
                None, _store.get_project, project_id
            )
            if not proj:
                logger.warning("[Updater] 依赖图重建跳过：项目 %s 不存在", project_id)
                return
            ppath = proj.get("path") or proj.get("repo_path")
            if not ppath:
                logger.warning("[Updater] 依赖图重建跳过：项目 %s 无路径", project_id)
                return
            if not await loop.run_in_executor(None, is_codegraph_installed):
                logger.info("[Updater] 依赖图重建跳过：codegraph 未安装")
                return

            cg_result = await loop.run_in_executor(
                None, run_codegraph_full, ppath
            )
            edges = getattr(cg_result, "edges", None) or []
            if not edges:
                logger.info("[Updater] 依赖图重建：%s 无依赖边，跳过", project_id)
                return
            # C1 治本：DELETE 旧边(原走 async 连接) + INSERT 新边(原走 sync 池)跨连接非原子，
            # 中途崩溃只删不写 → 依赖图空。合到 _replace_dependency_graph 的单 sync 事务(原子)。
            # self._lock 串行化并发重建任务（重建是 fire-and-forget，可能多个项目/多轮重叠 DELETE-
            # all+INSERT-all 互相中间可见）；调用点未持锁，无重入死锁。注：与 _index_file 的逐文件
            # 增量依赖写非同锁——那类交错由全量重建(每 N 变更)自愈，故不为其加重锁拖慢索引。
            async with self._lock:
                await loop.run_in_executor(
                    None, _replace_dependency_graph, project_id, edges
                )
            logger.info(
                "[Updater] 项目 %s 依赖图重建完成(%d 边)", project_id, len(edges)
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Updater] 依赖图重建失败(忽略): %s (%s)", project_id, exc)
            from swarm.infra.degrade import record_degrade
            record_degrade("knowledge.depgraph_rebuild")  # E1

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
                    # 复核 storage#6 治本：文件读不到(删除/移动/工作区缺失)旧代码 continue → 既不成功也
                    # 不 retry_count++ → 永久占坑，与"≥10 放弃"脱节。改为累加 retry_count(带 last_error)，
                    # 达上限后不再被选中(可人工清理)，与下方 except 分支同源收敛，杜绝无限占坑。
                    async with self._conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE kb_pending_embeddings
                            SET retry_count = retry_count + 1, last_error = %s
                            WHERE project_id=%s AND file_path=%s
                            """,
                            ("file unreadable/missing in workspace", pid, file_path),
                        )
                    continue
                try:
                    # write-then-prune（替代先删后索引）：index 失败保留旧 chunk 无空窗，
                    # 失败时该行仍留在 kb_pending_embeddings（下方 except 只 retry_count++）下轮重试。
                    await self._semantic.reindex_file_atomic(
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
                # R54-3（round54 实锤）：**必须显式写 event_type**。代码 DDL 声明它
                # `DEFAULT 'push'`，但线上真表是 `NOT NULL` 且**无默认值**（schema 漂移——
                # 表早已存在，`CREATE TABLE IF NOT EXISTS` 从未生效）→ 每一次入队都
                # NotNullViolation → 被调用方 logger.debug 静默吞掉 → **知识库增量回灌链路
                # 从未成功过一次**（kb_update_events / kb_modification_log / kb_co_occurrence
                # 三张表全空，retrieve_for_brain 的 behavior 面五轮恒 0）。
                # 不靠默认值（漂移的 schema 不可信），坐标由调用方语义决定。
                _etype = str((event.metadata or {}).get("source") or "push")
                await cur.execute(
                    """
                    INSERT INTO kb_update_events (project_id, event_type, payload_json)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (event.project_id, _etype, psycopg.types.json.Jsonb(payload)),
                )
                row = await cur.fetchone()
        return row[0]

    # ── D39：卡死事件对账（stale processing / failed 有界重放）────────

    @staticmethod
    def _stale_processing_seconds() -> int:
        """stale processing 判定阈值（秒）。env SWARM_KB_STALE_PROCESSING_SEC，
        非法值回退默认 300，钳制 [1, 86400]。"""
        raw = os.environ.get("SWARM_KB_STALE_PROCESSING_SEC", "")
        try:
            val = int(raw) if raw else 300
        except ValueError:
            val = 300
        return min(86400, max(1, val))

    @staticmethod
    def _failed_max_retries() -> int:
        """failed/stale-processing 重放的有界重试上限。env SWARM_KB_FAILED_MAX_RETRIES，
        非法值回退默认 3，钳制 [0, 100]（0=不重试，只把 stale processing 显式转 failed）。"""
        raw = os.environ.get("SWARM_KB_FAILED_MAX_RETRIES", "")
        try:
            val = int(raw) if raw else 3
        except ValueError:
            val = 3
        return min(100, max(0, val))

    async def _maybe_reconcile_stuck(self) -> None:
        """节流包装：每 60s 至多对账一次（首次调用立即跑=startup 对账）。"""
        import time as _time

        now = _time.monotonic()
        last = getattr(self, "_last_stuck_reconcile_ts", 0.0)
        if last and now - last < 60.0:
            return
        self._last_stuck_reconcile_ts = now
        await self.reconcile_stuck_events()

    async def reconcile_stuck_events(self) -> dict:
        """D39 治本：恢复卡死的 kb_update_events，知识增量不再静默丢失。

        - 出队即置 processing、进程崩溃 → 行永停 processing 无人对账（全仓原无此恢复面）；
        - failed 无重试 → 单次抖动即永久丢增量。
        处置（事件为幂等重放安全的索引操作）：
          1) processing 且 claimed_at（缺则 created_at）超阈值、重试额度未尽 → 重置 pending
             并 retry_count+1（有界：防毒事件反复崩进程的无限循环）；
          2) 同上但重试耗尽 → 显式转 failed + error_message（可观测终态，不再空转）；
          3) failed 且额度未尽、上次处置已过阈值 → 重置 pending 重放（有界重试）；
             耗尽的保持 failed（可观测，人工介入面）。
        阈值/上限走 env（_stale_processing_seconds/_failed_max_retries）。返回各类计数。
        """
        assert self._conn
        stale_s = self._stale_processing_seconds()
        max_retries = self._failed_max_retries()
        stats = {"processing_reset": 0, "processing_exhausted": 0, "failed_retried": 0}
        async with self._lock:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE kb_update_events
                    SET status = 'pending', retry_count = COALESCE(retry_count, 0) + 1
                    WHERE status = 'processing'
                      AND COALESCE(claimed_at, created_at) < now() - make_interval(secs => %s)
                      AND COALESCE(retry_count, 0) < %s
                    RETURNING id
                    """,
                    (stale_s, max_retries),
                )
                stats["processing_reset"] = len(await cur.fetchall())
                await cur.execute(
                    """
                    UPDATE kb_update_events
                    SET status = 'failed',
                        error_message = left(COALESCE(error_message, '')
                                             || ' [stale processing: retries exhausted]', 800),
                        processed_at = now()
                    WHERE status = 'processing'
                      AND COALESCE(claimed_at, created_at) < now() - make_interval(secs => %s)
                      AND COALESCE(retry_count, 0) >= %s
                    RETURNING id
                    """,
                    (stale_s, max_retries),
                )
                stats["processing_exhausted"] = len(await cur.fetchall())
                await cur.execute(
                    """
                    UPDATE kb_update_events
                    SET status = 'pending', retry_count = COALESCE(retry_count, 0) + 1
                    WHERE status = 'failed'
                      AND COALESCE(processed_at, claimed_at, created_at)
                          < now() - make_interval(secs => %s)
                      AND COALESCE(retry_count, 0) < %s
                    RETURNING id
                    """,
                    (stale_s, max_retries),
                )
                stats["failed_retried"] = len(await cur.fetchall())
        if any(stats.values()):
            logger.warning(
                "[Updater] D39 卡死事件对账：stale processing 重置 %d、耗尽转 failed %d、"
                "failed 有界重试 %d（阈值 %ds，上限 %d 次）",
                stats["processing_reset"], stats["processing_exhausted"],
                stats["failed_retried"], stale_s, max_retries,
            )
        return stats

    async def process_pending_events(self, batch_size: int = 10) -> int:
        """处理队列中的待处理事件（~5s 窗口批量合并）。

        调度器每 5 秒轮询一次，本次拉出的就是过去 ~5s 累积的事件。
        按 project_id 分组后合并去重，同一项目的多个事件合为一批处理，
        减少重复索引开销。某项目处理失败不影响其他项目。
        """
        assert self._conn
        processed = 0

        # D39：先跑（节流的）卡死对账——进程崩溃留下的 stale processing / failed 事件
        # 有界重放，否则知识增量静默永久丢失。首次调用（startup 后第一轮）必跑。
        try:
            await self._maybe_reconcile_stuck()
        except Exception as exc:  # noqa: BLE001 — 对账失败不阻断正常消费
            logger.warning("[Updater] D39 卡死事件对账失败（跳过本轮，不阻断消费）: %s", exc)

        # 串行化：与 enqueue_event 共享同一 AsyncConnection，不能并发查询
        async with self._lock:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE kb_update_events
                    SET status = 'processing', claimed_at = now()
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
                    _res = await self.handle_event(merged)
                    # #3：handle_event 把单文件/Layer 错误吞进 result["errors"] 不抛出。
                    # 若有错误（非 Layer B embedding 降级——那条走 kb_pending 重试队列、不入 errors），
                    # 不能标 done 假装成功（会静默丢索引）。标 failed + 错误摘要，至少可观测可排查。
                    _errs = _res.get("errors") if isinstance(_res, dict) else None
                    if _errs:
                        _summary = "; ".join(
                            f"{e.get('file') or e.get('layer') or '?'}: {e.get('error', '')}"
                            for e in _errs
                        )[:500]
                        async with self._conn.cursor() as cur:
                            await cur.execute(
                                """
                                UPDATE kb_update_events
                                SET status = 'failed', error_message = %s, processed_at = now()
                                WHERE id = ANY(%s)
                                """,
                                (f"partial failure: {_summary}", event_ids),
                            )
                    else:
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
