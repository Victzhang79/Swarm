#!/usr/bin/env python3
"""周期全量重预处理调度的 staleness 判定单测（纯逻辑，不跑真预处理）。"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.knowledge.scheduler import _is_stale


def test_no_progress_is_stale():
    assert _is_stale(None, 24.0) is True
    print("  ✅ 无预处理记录 → stale")


def test_running_not_stale():
    # 正在跑/非 complete 阶段不碰
    assert _is_stale({"phase": "indexing"}, 24.0) is False
    print("  ✅ 进行中不重复触发")


def test_complete_recent_not_stale():
    now = datetime.now(timezone.utc)
    assert _is_stale({"phase": "complete", "completed_at": now - timedelta(hours=2)}, 24.0) is False
    print("  ✅ 刚跑完(2h) < 24h 阈值 → 不重跑")


def test_complete_old_is_stale():
    now = datetime.now(timezone.utc)
    assert _is_stale({"phase": "complete", "completed_at": now - timedelta(hours=30)}, 24.0) is True
    print("  ✅ 30h 前完成 > 24h → stale")


def test_complete_no_timestamp_is_stale():
    assert _is_stale({"phase": "complete", "completed_at": None}, 24.0) is True
    print("  ✅ complete 但无时间戳 → stale(保守重跑)")


def test_iso_string_timestamp():
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    assert _is_stale({"phase": "complete", "completed_at": old}, 24.0) is True
    print("  ✅ ISO 字符串时间戳解析")


def test_naive_timestamp_treated_utc():
    # 无 tzinfo 的时间按 UTC 处理，不崩
    naive = datetime.utcnow() - timedelta(hours=1)
    assert _is_stale({"phase": "complete", "completed_at": naive}, 24.0) is False
    print("  ✅ naive datetime 当 UTC 处理")


def main():
    tests = [
        test_no_progress_is_stale,
        test_running_not_stale,
        test_complete_recent_not_stale,
        test_complete_old_is_stale,
        test_complete_no_timestamp_is_stale,
        test_iso_string_timestamp,
        test_naive_timestamp_treated_utc,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
