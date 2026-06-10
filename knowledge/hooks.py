"""任务生命周期触发的知识库副作用。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from swarm.knowledge.scheduler import enqueue_kb_update
from swarm.knowledge.updater import ChangeType, FileChange, UpdateEvent
from swarm.project.diff_apply import files_from_unified_diff

logger = logging.getLogger(__name__)


def _build_changes(project_path: str, merged_diff: str) -> list[FileChange]:
    root = Path(project_path)
    changes: list[FileChange] = []
    for rel in files_from_unified_diff(merged_diff):
        full = root / rel
        if not full.is_file():
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("skip knowledge update for %s: %s", rel, exc)
            continue
        changes.append(
            FileChange(
                file_path=rel.replace("\\", "/"),
                change_type=ChangeType.MODIFIED,
                content=content,
                diff=merged_diff,
            )
        )
    return changes


def _build_webhook_changes(project_path: str, payload: dict) -> list[FileChange]:
    """解析 webhook payload，跨 commit 去重（同路径保留最后状态）。"""
    root = Path(project_path)
    by_path: dict[str, FileChange] = {}
    order: list[str] = []

    for commit in payload.get("commits") or []:
        removed = {p.replace("\\", "/") for p in (commit.get("removed") or [])}
        added = {p.replace("\\", "/") for p in (commit.get("added") or [])}
        modified = {p.replace("\\", "/") for p in (commit.get("modified") or [])}

        for rel in removed:
            if rel not in by_path:
                order.append(rel)
            by_path[rel] = FileChange(file_path=rel, change_type=ChangeType.DELETED)

        for rel in sorted(added | modified):
            if rel in removed:
                continue
            full = root / rel
            if not full.is_file():
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            ctype = ChangeType.ADDED if rel in added else ChangeType.MODIFIED
            if rel not in by_path:
                order.append(rel)
            by_path[rel] = FileChange(
                file_path=rel,
                change_type=ctype,
                content=content,
                diff=commit.get("message"),
            )

    return [by_path[p] for p in order if p in by_path]


async def incremental_update_from_task(
    project_id: str,
    project_path: str,
    merged_diff: str,
    *,
    task_id: str | None = None,
) -> dict | None:
    """任务 accept 后，对变更文件入队 Layer A/B/D 增量索引。"""
    changes = _build_changes(project_path, merged_diff)
    if not changes:
        return None

    event_id = await enqueue_kb_update(
        UpdateEvent(
            project_id=project_id,
            task_id=task_id,
            changes=changes,
            metadata={"project_path": project_path, "source": "task_accept"},
        )
    )
    return {"status": "queued", "event_id": event_id, "total_changes": len(changes)}


def schedule_incremental_update(
    project_id: str,
    project_path: str,
    merged_diff: str,
    *,
    task_id: str | None = None,
) -> None:
    """后台触发增量更新（不阻塞 approve 响应）。"""

    async def _run() -> None:
        try:
            result = await incremental_update_from_task(
                project_id,
                project_path,
                merged_diff,
                task_id=task_id,
            )
            if result:
                logger.info(
                    "knowledge update queued task=%s event_id=%s files=%s",
                    task_id,
                    result.get("event_id"),
                    result.get("total_changes"),
                )
        except Exception:
            logger.exception("incremental knowledge update failed task=%s", task_id)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        asyncio.run(_run())


async def handle_git_push_webhook(
    project_id: str,
    project_path: str,
    payload: dict,
) -> dict:
    """Git push webhook — 解析 commits 并入队增量索引。"""
    changes = _build_webhook_changes(project_path, payload)
    if not changes:
        return {"status": "noop", "changes": 0}

    commit_hash = ""
    for commit in payload.get("commits") or []:
        commit_hash = commit.get("id") or commit_hash
    author = payload.get("user_name") or (payload.get("author") or {}).get("name")

    event_id = await enqueue_kb_update(
        UpdateEvent(
            project_id=project_id,
            commit_hash=commit_hash,
            author=author,
            changes=changes,
            metadata={"project_path": project_path, "source": "git_webhook"},
        )
    )
    return {"status": "queued", "event_id": event_id, "total_changes": len(changes)}
