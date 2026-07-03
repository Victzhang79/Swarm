"""任务生命周期触发的知识库副作用。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from swarm.knowledge.scheduler import enqueue_kb_update
from swarm.knowledge.updater import ChangeType, FileChange, UpdateEvent
from swarm.project.diff_apply import files_from_unified_diff

logger = logging.getLogger(__name__)

# P2：后台任务强引用集。loop.create_task 的返回值若不被持有，任务可能在完成前被 GC，
# 异常也随之丢失。把任务存进模块级集合保活，并在完成回调里移除 + 暴露异常。
_BG_TASKS: set[asyncio.Task] = set()


def _track_bg_task(task: asyncio.Task) -> None:
    _BG_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _BG_TASKS.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error("[hooks] 后台知识库更新任务异常: %r", exc)

    task.add_done_callback(_done)


def _build_changes(project_path: str, merged_diff: str) -> list[FileChange]:
    """从 merged_diff 涉及文件构建 KB 变更集。

    ★对抗复核 3rd#1 前提★：本函数读【磁盘当前内容】——必须在【产出已 apply+commit 之后】调用
    （learn_success commit 后），否则读到的是 L2 回滚后的 HEAD 旧内容 → 知识库被旧代码覆盖。
    删除文件（diff 里有、磁盘已不在）→ 发 DELETED 让 updater 清除旧向量，不再残留。
    """
    root = Path(project_path)
    changes: list[FileChange] = []
    for rel in files_from_unified_diff(merged_diff):
        rel_norm = rel.replace("\\", "/")
        full = root / rel
        if not full.is_file():
            # 磁盘不存在 = 该文件被删除 → 显式 DELETED（清旧向量），不再静默跳过留残留。
            changes.append(FileChange(file_path=rel_norm, change_type=ChangeType.DELETED))
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("skip knowledge update for %s: %s", rel, exc)
            continue
        changes.append(
            FileChange(
                file_path=rel_norm,
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
        _track_bg_task(loop.create_task(_run()))  # P2：保活 + 异常暴露，非 fire-and-forget
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
