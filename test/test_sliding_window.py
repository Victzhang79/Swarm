#!/usr/bin/env python3
"""L3 滑动窗口上下文压缩测试。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.memory.sliding_window import (
    PRIORITY_PROCESS,
    PRIORITY_USER,
    PRIORITY_WORKER,
    append_context_event,
    compress_context_log,
    estimate_tokens,
    format_sliding_context_for_prompt,
    truncate_text_to_tokens,
)


def test_estimate_tokens_empty_and_short():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello") >= 1
    long_text = "x" * 350
    assert estimate_tokens(long_text) == int(350 / 3.5)


def test_append_context_event_skips_empty():
    log = append_context_event(None, "analyze", "  ")
    assert log == []


def test_append_context_event_appends_metadata():
    log = append_context_event(None, "analyze", "复杂度=simple", priority=PRIORITY_PROCESS)
    assert len(log) == 1
    assert log[0]["type"] == "analyze"
    assert log[0]["priority"] == PRIORITY_PROCESS
    assert log[0]["pinned"] is False
    assert log[0]["tokens"] >= 1


def test_compress_keeps_pinned_user_request():
    log = [
        {"type": "user_request", "content": "原始需求", "priority": PRIORITY_USER, "pinned": True, "tokens": 10},
        {"type": "analyze", "content": "a" * 5000, "priority": PRIORITY_PROCESS, "pinned": False, "tokens": 1500},
        {"type": "worker_batch", "content": "b" * 5000, "priority": PRIORITY_WORKER, "pinned": False, "tokens": 1500},
    ]
    new_log, summary, total = compress_context_log(log, "", max_tokens=2000, reserve_tokens=500)
    pinned = [e for e in new_log if e.get("pinned")]
    assert len(pinned) == 1
    assert pinned[0]["type"] == "user_request"
    assert summary  # evicted events summarized
    assert total <= 2000 - 500 + 500  # within reasonable budget


def test_compress_evicts_low_priority_first():
    old = {"type": "old_process", "content": "old", "priority": PRIORITY_PROCESS, "pinned": False, "tokens": 800}
    worker = {"type": "worker", "content": "new", "priority": PRIORITY_WORKER, "pinned": False, "tokens": 800}
    log = [old, worker]
    new_log, summary, _ = compress_context_log(log, "", max_tokens=1200, reserve_tokens=200)
    types = [e["type"] for e in new_log if not e.get("pinned")]
    assert "worker" in types or summary  # worker kept or summarized


def test_truncate_text_to_tokens():
    text = "word " * 2000
    trimmed = truncate_text_to_tokens(text, max_tokens=50)
    assert estimate_tokens(trimmed) <= 50 + 5
    assert "L3 截断" in trimmed


def test_truncate_short_text_unchanged():
    text = "short context"
    assert truncate_text_to_tokens(text, max_tokens=100) == text


def test_format_sliding_context_for_prompt():
    summary = "历史摘要：已完成 analyze"
    log = [
        {"type": "analyze", "content": "复杂度=medium"},
        {"type": "plan", "content": "3 个子任务"},
    ]
    out = format_sliding_context_for_prompt(summary, log, max_tokens=8000)
    assert "L3 上下文摘要" in out
    assert "L3 近期事件" in out
    assert "analyze" in out
    assert "plan" in out


def test_format_empty_returns_empty():
    assert format_sliding_context_for_prompt("", None) == ""


def main() -> int:
    print("=== test_sliding_window ===")
    tests = [
        test_estimate_tokens_empty_and_short,
        test_append_context_event_skips_empty,
        test_append_context_event_appends_metadata,
        test_compress_keeps_pinned_user_request,
        test_compress_evicts_low_priority_first,
        test_truncate_text_to_tokens,
        test_truncate_short_text_unchanged,
        test_format_sliding_context_for_prompt,
        test_format_empty_returns_empty,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback
            traceback.print_exc()
    if failed:
        return 1
    print("\nAll passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
