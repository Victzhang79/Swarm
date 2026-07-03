#!/usr/bin/env python3
"""#8 进度分母 stale → ELABORATE 拆分后回写 subtask_count/plan（round20 治本）回归测试。

背景（round19 review:29，用户现场发现 WebUI 0/35）：PLAN 生成 35 子任务，ELABORATE 二次拆分到 64
派发单元并 `out["plan"]=plan_obj` 回写 state，但 runner 的 `_sync_task_from_state` 只在
`("analyze","plan","merge","verify_l3","dispatch")` 节点 on_chain_end 触发——【漏了 elaborate】。
且它收的是【该节点 output 增量】非全量 state：dispatch 虽在列表但其 output 不含 "plan" 键 →
subtask_count 停在 PLAN 的 35 → WebUI 分母 35 ≠ 真实 64（c5646a5 三本账修的是语义，分母源没修）。

治本：① 把触发节点抽为模块常量 `_SYNC_ON_NODES` 并【加入 "elaborate"】；② _sync_task_from_state
本身已能从 output["plan"] 正确导出 subtask_count（本测确证）。elaborate 未拆分时不 emit "plan"→
plan is None→subtask_count 块跳过→保持既有值，无副作用。

本套验证：① elaborate 在触发列表；② 给 64 子任务 plan → 回写 subtask_count=64 + plan 字段；
③ 无 plan 的 output（如 dispatch 增量）→ 不动 subtask_count（不误清）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import runner  # noqa: E402


def test_elaborate_in_sync_nodes():
    assert "elaborate" in runner._SYNC_ON_NODES, "elaborate 必须在进度回写触发列表"
    # 既有节点未回归
    for n in ("analyze", "plan", "merge", "verify_l3", "dispatch"):
        assert n in runner._SYNC_ON_NODES
    print("  ✅ ① elaborate 已入 _SYNC_ON_NODES（既有节点未回归）")


def test_sync_writes_64_from_elaborate_output(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: captured.update(kw))
    # elaborate 的 output 增量：含拆分后 64 子任务的 plan
    out = {
        "plan": {"subtasks": [{"id": f"st-{i}"} for i in range(64)]},
        "plan_elaborated": True,
    }
    runner._sync_task_from_state("t-1", out)
    assert captured.get("subtask_count") == 64, "ELABORATE 拆分后分母应回写为 64"
    assert captured.get("plan", {}).get("subtasks"), "plan 字段应同步回写"
    print("  ✅ ② elaborate output(64 子任务) → subtask_count=64 + plan 回写")


def test_sync_without_plan_leaves_count_untouched(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: captured.update(kw))
    # 如 dispatch 增量：无 "plan" 键 → 不应写 subtask_count（保持既有 35，不误清/误改）
    out = {"subtask_results": {}, "dispatch_remaining": ["st-1"]}
    runner._sync_task_from_state("t-2", out)
    assert "subtask_count" not in captured, "无 plan 的 output 不应触碰 subtask_count"
    print("  ✅ ③ 无 plan 的 output（dispatch 增量）→ 不动 subtask_count（无副作用）")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
