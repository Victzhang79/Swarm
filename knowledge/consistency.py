"""ConsistencyChecker — 知识库索引 vs 工作区文件一致性（设计文档 P1）。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

from swarm.config.settings import get_config
from swarm.project.preprocess import EXCLUDED_DIRS, EXCLUDED_EXTENSIONS

logger = logging.getLogger(__name__)


def _conn_str() -> str:
    return get_config().db.postgres_uri


def _is_source_file(path: Path, *, root: Path, gate: Any | None = None) -> bool:
    """本通道的入库候选判据——★必须与全量/增量通道同源★（26 号文 S-3）。

    历史病灶：本函数原本只查 EXCLUDED_DIRS/EXCLUDED_EXTENSIONS，**没有** hidden-file 检查，
    于是全量 preprocess 挡掉的 `.claude/*`、`.hermes/plans/*`、`.env` 等被一致性修复
    当作 `missing_index` **重新塞回索引**——准入面倒挂，三套过滤不同源的必然结果。
    接上 ingest_guard 后还有第二层意义：入库闸拒绝的文件若在此仍被判 missing，就会
    形成"巡检入队 → 入库闸拒绝 → 下次仍判 missing"的**无限重试环**。同源即杜绝该环。

    ★W1 复核 HIGH-1/MEDIUM 整改★：① 判据必须含**降噪层**（.gitignore）——否则
    `logs/*.log` 这类每天都在变的文件恒判 stale/missing、恒入队、恒被重新嵌入，
    正是"每天打满 200 预算从不收敛"的机制本体；② `root` 改**必填**（原先默认 None 时
    `rel` 退化成 `path.name`，hidden_dir 判据永久失效——安全 backstop 的 kwarg 必填是
    B8-F2 定下的范式）；③ 异常不再静默 `pass`。
    """
    if not path.is_file():
        return False
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    if path.suffix.lower() in EXCLUDED_EXTENSIONS:
        return False
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        logger.warning("[CONSISTENCY] 路径越出项目根，跳过：%s（root=%s）", path, root)
        return False
    try:
        if gate is None:
            from swarm.knowledge.ingest_guard import reject_reason_by_name
            return reject_reason_by_name(rel) is None
        return gate.reject_for_indexing(rel) is None
    except Exception as exc:  # noqa: BLE001
        _warn_once_guard_unavailable(exc)
        return False   # fail-closed：判据不可用时不把文件当入库候选（绝不反向灌库）


_guard_warned = False


def _warn_once_guard_unavailable(exc: object) -> None:
    global _guard_warned
    if not _guard_warned:
        _guard_warned = True
        logger.warning("[CONSISTENCY] 入库判据不可用 → 本轮不产出入库候选（fail-closed）: %s", exc)


def check_project_consistency(
    project_id: str, project_path: str, *, display_limit: int | None = 100
) -> dict[str, Any]:
    """比对磁盘 mtime 与 kb 索引记录，返回 stale / missing 文件列表。

    display_limit：stale_files/missing_index 在返回里的展示上限（默认 100，避免 API 响应
    巨大）。**修复(P2)**：repair 路径传 None 取【全量】列表——旧实现 repair 直接吃 [:100]
    截断后的展示列表，导致 stale 文件 >100 时每次只修前 100、其余永不被纳入修复目标，
    无法收敛。display_limit=None → 不截断（供 repair 用全量目标）。
    """
    root = Path(project_path)
    if not root.is_dir():
        return {"ok": False, "error": "project path not found", "stale_files": [], "missing_index": []}

    indexed: dict[str, datetime | None] = {}
    from swarm.infra.db import pg_connect_timeout_kwargs

    # D15：直连补 connect_timeout（不加 autocommit，保持原事务语义）。
    with psycopg.connect(_conn_str(), **pg_connect_timeout_kwargs()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT file_path, last_modified
                FROM kb_file_index
                WHERE project_id = %s
                """,
                (project_id,),
            )
            for fp, last_modified in cur.fetchall():
                indexed[str(fp)] = last_modified

    stale_files: list[dict[str, Any]] = []
    missing_index: list[str] = []
    checked = 0

    # 一次构造、全程复用：GitignoreFilter 内含仓库归属缓存，逐文件重建会退化成
    # 每文件一次 git 调用（W1 复核 HIGH-1）。
    try:
        from swarm.knowledge.ingest_guard import IngestGate
        _gate = IngestGate(root)
    except Exception as exc:  # noqa: BLE001
        _warn_once_guard_unavailable(exc)
        _gate = None

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if not _is_source_file(path, root=root, gate=_gate):
            continue
        checked += 1
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if rel not in indexed:
            missing_index.append(rel)
            continue
        db_ts = indexed[rel]
        if db_ts and db_ts.tzinfo is None:
            db_ts = db_ts.replace(tzinfo=timezone.utc)
        if db_ts and mtime > db_ts:
            stale_files.append({
                "file_path": rel,
                "disk_mtime": mtime.isoformat(),
                "indexed_at": db_ts.isoformat() if db_ts else None,
            })

    return {
        "ok": True,
        "checked_files": checked,
        "indexed_count": len(indexed),
        "stale_count": len(stale_files),
        "missing_index_count": len(missing_index),
        "stale_files": stale_files if display_limit is None else stale_files[:display_limit],
        "missing_index": missing_index if display_limit is None else missing_index[:display_limit],
        "recommendation": (
            "run preprocess or POST /knowledge/consistency-check?repair=true"
            if stale_files or missing_index
            else "consistent"
        ),
    }


async def repair_project_consistency(
    project_id: str,
    project_path: str,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """根据 consistency 报告修复 stale / missing 索引（入队增量更新）。"""
    from swarm.knowledge.scheduler import enqueue_kb_update
    from swarm.knowledge.updater import ChangeType, FileChange, UpdateEvent

    # P2：① 取【全量】列表(display_limit=None)，让 repair 据 limit 真正推进收敛，
    # 不被展示截断卡在前 100；② 同步扫描(psycopg.connect + rglob)走 to_thread，
    # 不阻塞事件循环。
    report = await asyncio.to_thread(
        check_project_consistency, project_id, project_path, display_limit=None
    )
    if not report.get("ok"):
        return report

    root = Path(project_path)
    targets: list[str] = []
    targets.extend(report.get("missing_index") or [])
    targets.extend(item["file_path"] for item in (report.get("stale_files") or []))
    targets = list(dict.fromkeys(targets))[:limit]
    # 返回给调用方的报告仍按展示上限截断，避免响应体过大
    report = {
        **report,
        "stale_files": (report.get("stale_files") or [])[:100],
        "missing_index": (report.get("missing_index") or [])[:100],
    }

    if not targets:
        return {**report, "repair": {"status": "noop", "queued": 0}}

    changes: list[FileChange] = []
    for rel in targets:
        full = root / rel
        if not full.is_file():
            changes.append(FileChange(file_path=rel, change_type=ChangeType.DELETED))
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        changes.append(
            FileChange(
                file_path=rel,
                change_type=ChangeType.MODIFIED,
                content=content,
            )
        )

    if not changes:
        return {**report, "repair": {"status": "noop", "queued": 0}}

    event_id = await enqueue_kb_update(
        UpdateEvent(
            project_id=project_id,
            changes=changes,
            metadata={"project_path": project_path, "source": "consistency_repair"},
        )
    )
    return {
        **report,
        "repair": {
            "status": "queued",
            "event_id": event_id,
            "queued": len(changes),
        },
    }


async def run_daily_consistency_all_projects(*, repair: bool = False) -> None:
    """每日扫描所有项目（由 API startup 调度）。"""
    from swarm.project import store

    try:
        projects = store.list_projects()
    except Exception as exc:
        logger.warning("[ConsistencyChecker] list projects failed: %s", exc)
        return

    for p in projects:
        pid = p.get("id")
        path = p.get("path")
        if not pid or not path:
            continue
        try:
            if repair:
                report = await repair_project_consistency(pid, path)
                if report.get("repair", {}).get("queued"):
                    logger.info(
                        "[ConsistencyChecker] project=%s repair queued=%s",
                        pid,
                        report["repair"].get("queued"),
                    )
            else:
                # P2：同步扫描走 to_thread，不阻塞每日巡检所在的事件循环
                report = await asyncio.to_thread(check_project_consistency, pid, path)
                if report.get("stale_count") or report.get("missing_index_count"):
                    logger.warning(
                        "[ConsistencyChecker] project=%s stale=%s missing=%s",
                        pid,
                        report.get("stale_count"),
                        report.get("missing_index_count"),
                    )
        except Exception as exc:
            logger.warning("[ConsistencyChecker] project=%s failed: %s", pid, exc)
