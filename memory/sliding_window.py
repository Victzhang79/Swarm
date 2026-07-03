"""L3 滑动窗口 — 任务执行期上下文压缩（Memory L3，非验证 V3）。"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 字符 ≈ token 启发式（中英文混合）
CHARS_PER_TOKEN = 3.5

PRIORITY_USER = 1       # 用户原始需求 — 永不丢弃
PRIORITY_WORKER = 2     # Worker 最新产出
PRIORITY_PROCESS = 3    # Brain 中间过程


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def default_max_tokens() -> int:
    return int(os.environ.get("SWARM_CONTEXT_MAX_TOKENS", "80000"))


def default_reserve_tokens() -> int:
    return int(os.environ.get("SWARM_CONTEXT_RESERVE_TOKENS", "16000"))


def append_context_event(
    log: list[dict[str, Any]] | None,
    event_type: str,
    content: str,
    *,
    priority: int = PRIORITY_PROCESS,
    pinned: bool = False,
) -> list[dict[str, Any]]:
    """追加一条上下文事件到 L3 log。"""
    log = list(log or [])
    text = (content or "").strip()
    if not text:
        return log
    log.append({
        "type": event_type,
        "content": text[:4000],
        "priority": priority,
        "pinned": pinned,
        "tokens": estimate_tokens(text[:4000]),
    })
    return log


def _summarize_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return ""
    lines = ["[L3 历史上下文摘要]"]
    for e in events:
        et = e.get("type", "event")
        snippet = str(e.get("content", ""))[:180].replace("\n", " ")
        lines.append(f"- ({et}) {snippet}")
    return "\n".join(lines)[:8000]


def compress_context_log(
    log: list[dict[str, Any]] | None,
    summary: str = "",
    *,
    max_tokens: int | None = None,
    reserve_tokens: int | None = None,
) -> tuple[list[dict[str, Any]], str, int]:
    """压缩 L3 上下文：老消息摘要化，保留 pinned + 高优先级近期事件。

    Returns:
        (new_log, new_summary, total_tokens)
    """
    max_tokens = max_tokens if max_tokens is not None else default_max_tokens()
    reserve_tokens = reserve_tokens if reserve_tokens is not None else default_reserve_tokens()
    budget = max(1000, max_tokens - reserve_tokens)

    log = list(log or [])
    pinned = [e for e in log if e.get("pinned")]
    rest = [e for e in log if not e.get("pinned")]

    def total_tk(events: list[dict], summ: str) -> int:
        return estimate_tokens(summ) + sum(int(e.get("tokens") or estimate_tokens(e.get("content", ""))) for e in events)

    evicted: list[dict] = []
    while rest and total_tk(pinned + rest, summary) > budget:
        # 先 evict 最低 priority 的最旧事件。
        # 修复(P2)：USER 原始需求(PRIORITY_USER=1)【永不逐出】。旧实现当 rest 中无
        # priority>=PRIORITY_PROCESS 的事件时 victim_idx 兜底为 0，排序后 rest[0] 恰是最低
        # priority(可能就是 USER)→ USER 被静默逐出，违反"永不丢弃用户需求"契约。
        # 新规则：只在 priority>PRIORITY_USER 的事件里挑最旧的逐出；若没有可逐出者
        # （全是 USER），宁可超预算也 break，绝不动 USER。
        # P1-11 治本：数字大=价值低（USER=1 最珍贵永不逐出、WORKER=2 产出、PROCESS=3 中间过程）。
        # 应先逐出【价值最低=priority 数字最大】的最旧事件。旧实现升序取 rest[0]>USER 反而先逐
        # WORKER(2) 再 PROCESS(3)，方向反了。改：降序(数字大在前)、稳定排序保同级最旧在前。
        rest.sort(key=lambda e: -e.get("priority", PRIORITY_PROCESS))
        if rest and rest[0].get("priority", PRIORITY_PROCESS) > PRIORITY_USER:
            evicted.append(rest.pop(0))
        else:
            break  # 只剩 USER，拒绝逐出（接受预算溢出）

    if evicted:
        chunk = _summarize_events(evicted)
        summary = (summary + "\n" + chunk).strip() if summary else chunk
        logger.info("[L3] compressed %d events into summary (%d tokens)", len(evicted), estimate_tokens(summary))

    # summary 本身过长则截断
    while estimate_tokens(summary) > budget // 3 and summary:
        summary = summary[: max(0, len(summary) - 500)]

    new_log = pinned + rest
    total = total_tk(new_log, summary)
    return new_log, summary, total


def truncate_text_to_tokens(text: str, max_tokens: int) -> str:
    """截断文本到 token 预算。"""
    if estimate_tokens(text) <= max_tokens:
        return text
    max_chars = int(max_tokens * CHARS_PER_TOKEN)
    trimmed = text[:max_chars]
    return trimmed + "\n\n…（L3 截断：上下文过长）"


def format_sliding_context_for_prompt(
    summary: str,
    log: list[dict[str, Any]] | None,
    *,
    max_tokens: int | None = None,
) -> str:
    """格式化为可注入 Brain prompt 的 L3 段落。"""
    max_tokens = max_tokens if max_tokens is not None else default_max_tokens()
    parts: list[str] = []
    if summary:
        parts.append(f"### L3 上下文摘要\n{summary[:6000]}")
    if log:
        parts.append("### L3 近期事件")
        for e in log[-12:]:
            parts.append(f"- **{e.get('type', 'event')}**: {str(e.get('content', ''))[:500]}")
    text = "\n\n".join(parts)
    return truncate_text_to_tokens(text, max_tokens // 4) if text else ""


def compress_state_context(state: dict[str, Any]) -> dict[str, Any]:
    """从 BrainState 字段运行 L3 压缩，返回 state 更新片段。"""
    log = state.get("context_log") or []
    summary = state.get("context_summary") or ""
    new_log, new_summary, total = compress_context_log(log, summary)
    return {
        "context_log": new_log,
        "context_summary": new_summary,
        "context_token_estimate": total,
    }
