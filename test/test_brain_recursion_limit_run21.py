#!/usr/bin/env python3
"""RUN21 回归：Brain graph recursion_limit 解析。

根因：新任务首次 invoke 时 complexity 与 subtask_count 都未知（图内 ANALYZE/PLAN 才生成），
旧实现两条放大分支都不命中 → 落低 floor(50) → 大 ultra 任务 + rebase 轮撞穿 GraphRecursionError。
修复：未知即按最坏情况 ultra 上限兜底。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_fresh_task_unknown_gets_high_limit():
    """新任务 invoke 时 complexity=None & subtask_count=None → 不再落 50，按 ultra 兜底。"""
    from swarm.tracing import resolve_brain_recursion_limit

    limit = resolve_brain_recursion_limit(None, None)
    assert limit >= 300, f"未知规模新任务应按 ultra 兜底(>=300)，实得 {limit}（RUN21 撞穿点）"
    print(f"  ✅ 未知规模新任务 recursion_limit={limit}（不再是 50）")


def test_empty_complexity_gets_high_limit():
    """空字符串 complexity（等价未知）→ 按 ultra 兜底。"""
    from swarm.tracing import resolve_brain_recursion_limit

    assert resolve_brain_recursion_limit("", None) >= 300
    print("  ✅ 空 complexity 按 ultra 兜底")


def test_known_small_complexity_not_inflated():
    """已知小任务（trivial/medium/simple）→ 仍走低 floor，不被误抬高（守住精准性）。"""
    from swarm.tracing import BRAIN_RECURSION_LIMIT, resolve_brain_recursion_limit

    assert resolve_brain_recursion_limit("trivial", None) == BRAIN_RECURSION_LIMIT
    assert resolve_brain_recursion_limit("simple", None) == BRAIN_RECURSION_LIMIT
    print("  ✅ 已知小任务不被抬高（仅真未知才按 ultra 兜底）")


def test_known_subtask_count_scales():
    """已知子任务数（resume/rerun）→ 4×+40 放大，覆盖 RUN21 的 37 子任务。"""
    from swarm.tracing import resolve_brain_recursion_limit

    assert resolve_brain_recursion_limit("ultra", 37) >= 37 * 4 + 40
    assert resolve_brain_recursion_limit(None, 50) >= 50 * 4 + 40
    print("  ✅ 已知子任务数仍按 4×+40 放大")


def test_known_complexity_respected():
    from swarm.tracing import resolve_brain_recursion_limit

    assert resolve_brain_recursion_limit("ultra", None) >= 300
    assert resolve_brain_recursion_limit("complex", None) >= 150
    print("  ✅ 已知复杂度档位仍生效")


def main() -> int:
    print("=== test_brain_recursion_limit_run21 ===")
    failed = 0
    for fn in (
        test_fresh_task_unknown_gets_high_limit,
        test_empty_complexity_gets_high_limit,
        test_known_small_complexity_not_inflated,
        test_known_subtask_count_scales,
        test_known_complexity_respected,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    print(f"\n{'All passed' if not failed else str(failed) + ' failed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
