#!/usr/bin/env python3
"""SIMPLE 路径 handle_failure 重试上限回归测试。

历史 bug(RuoYi e2e 暴露)：SIMPLE 分支无条件 retry，遇到"L1 通过但 diff 空"
(重试时本地文件已被上轮改过→difflib 基线已含变更→diff 空→判失败)会无限循环。
修复后引入与复杂路径一致的 subtask_retry_counts 硬上限 → 超限升级人工。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _run(coro):
    return asyncio.run(coro)


def test_simple_failure_retries_then_escalates():
    """SIMPLE 失败子任务重试有限次后升级人工，不无限循环。"""
    from swarm.brain.nodes import handle_failure
    from swarm.config.settings import get_config
    from swarm.types import Complexity

    max_retries = get_config().model.max_retries

    # 模拟同一子任务反复失败：累加 retry_counts 直到超限
    state = {
        "complexity": Complexity.SIMPLE,
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": {"l1_details": {}}},
        "dispatch_remaining": [],
        "subtask_retry_counts": {},
    }
    escalated = False
    for _ in range(max_retries + 5):  # 远超上限，必须在某次升级
        out = _run(handle_failure(dict(state)))
        state["subtask_retry_counts"] = out.get("subtask_retry_counts", state["subtask_retry_counts"])
        if out.get("failure_strategy") == "escalate":
            escalated = True
            break
        # 模拟下一轮仍失败
        state["failed_subtask_ids"] = ["st-1"]
    assert escalated, "SIMPLE 重试应在有限次后升级人工，而非无限 retry"
    print(f"  ✅ SIMPLE 失败重试 {max_retries}+1 次后升级人工(不死循环)")


def test_simple_failure_switches_to_alternate_model():
    """SIMPLE 重试超 max_retries 次后切换备选模型(retry_alternate)。"""
    from swarm.brain.nodes import handle_failure
    from swarm.config.settings import get_config
    from swarm.types import Complexity

    max_retries = get_config().model.max_retries
    # 预置已重试 max_retries 次 → 下一次应 forced_alternate
    state = {
        "complexity": Complexity.SIMPLE,
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": {"l1_details": {}}},
        "dispatch_remaining": [],
        "subtask_retry_counts": {"st-1": max_retries},
    }
    out = _run(handle_failure(state))
    assert out.get("failure_strategy") == "retry_alternate", f"应换备选模型，实际 {out.get('failure_strategy')}"
    assert out.get("use_alternate_model") is True
    print("  ✅ SIMPLE 超 max_retries 次 → 切换备选模型")


def main() -> int:
    print("=== test_simple_retry_cap ===")
    failed = 0
    for fn in (
        test_simple_failure_retries_then_escalates,
        test_simple_failure_switches_to_alternate_model,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed} failed")
        return 1
    print("\nAll passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
