"""L2 近期任务摘要 — 读取与 Brain prompt 格式化。"""

from __future__ import annotations

import logging
from typing import Any

from swarm.memory.store import MemoryStore

logger = logging.getLogger(__name__)

L2_READ_LIMIT = 10


async def load_recent_task_summaries(
    project_id: str,
    *,
    limit: int = L2_READ_LIMIT,
) -> list[dict[str, Any]]:
    if not project_id:
        return []
    store = MemoryStore()
    await store.connect()
    try:
        return await store.query_task_summaries(project_id, limit=limit)
    except Exception as exc:
        logger.warning("[L2] load recent summaries failed: %s", exc)
        return []
    finally:
        await store.close()


def format_recent_tasks_for_brain(summaries: list[dict[str, Any]]) -> str:
    """格式化为 Brain analyze/plan 可读文本。"""
    if not summaries:
        return "（无近期任务摘要；本项目尚无历史任务记录）"

    outcome_icon = {
        "success": "✅",
        "failure": "❌",
        "rejected": "🚫",
        "partial": "⚠️",
    }
    lines = [
        "## 近期任务摘要（L2 — 滚动清单，非原文）",
        "> 规划时参考用户最近在做什么，避免重复改动或模块冲突。",
        "",
    ]
    for s in summaries[:L2_READ_LIMIT]:
        outcome = s.get("outcome") or "unknown"
        icon = outcome_icon.get(outcome, "·")
        meta = s.get("metadata") or {}
        if isinstance(meta, str):
            meta = {}
        modules = meta.get("modules") or []
        mod_str = ", ".join(modules[:4]) if modules else "—"
        created = s.get("created_at", "")
        ts = str(created)[:10] if created else ""
        lines.append(
            f"- {icon} **{s.get('summary', '')[:140]}** "
            f"(模块: {mod_str}; {ts})"
        )
    return "\n".join(lines)
