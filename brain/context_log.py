"""Brain 节点 L3 上下文 log 辅助。"""

from __future__ import annotations

from typing import Any

from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.memory.sliding_window import (
    PRIORITY_PROCESS,
    PRIORITY_USER,
    append_context_event,
    compress_context_log,
    format_sliding_context_for_prompt,
)


def touch_context(
    state: BrainState,
    event_type: str,
    content: str,
    *,
    priority: int = PRIORITY_PROCESS,
    pinned: bool = False,
) -> dict[str, Any]:
    """追加 L3 事件并触发压缩，返回 state 更新片段。"""
    cfg = get_config()
    log = append_context_event(
        state.get("context_log"),
        event_type,
        content,
        priority=priority,
        pinned=pinned,
    )
    new_log, new_summary, total = compress_context_log(
        log,
        state.get("context_summary") or "",
        max_tokens=cfg.context_max_tokens,
        reserve_tokens=cfg.context_reserve_tokens,
    )
    return {
        "context_log": new_log,
        "context_summary": new_summary,
        "context_token_estimate": total,
    }


def sliding_context_prompt(state: BrainState) -> str:
    cfg = get_config()
    return format_sliding_context_for_prompt(
        state.get("context_summary") or "",
        state.get("context_log"),
        max_tokens=cfg.context_max_tokens,
    )


def init_task_context(state: BrainState) -> dict[str, Any]:
    """任务开始时 pinned 用户原始需求。"""
    desc = state.get("task_description") or ""
    return touch_context(
        state,
        "user_request",
        desc,
        priority=PRIORITY_USER,
        pinned=True,
    )
