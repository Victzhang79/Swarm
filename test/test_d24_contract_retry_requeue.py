#!/usr/bin/env python3
"""D24 治本单测 —— 契约失败 retry 分支重派+清结果；dispatch 早退分支 always-emit failed_ids。

旧 bug：
  (a) failure.py 契约分支只自增计数、返回 failed_subtask_ids=failed，既不 pop subtask_results 也不
      加回 dispatch_remaining（其它 retry 分支都做两步）→ 下轮 dispatch 见这些 id 仍在 completed →
      to_dispatch 空 → 早退 → monitor 读残留 failed 再进 handle_failure（verification_failure 已清）→
      走常规能力阶梯把 L1 全过的输出误诊断 pop 全部全量重跑。
  (b) dispatch 早退分支不回填 failed_subtask_ids（违反 always-emit）→ last-write-wins 通道残留旧 failed。
治本：契约分支对称 pop+加回 remaining、failed_ids 清空；dispatch 早退始终回填 failed_subtask_ids。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.nodes.dispatch import dispatch  # noqa: E402
from swarm.brain.nodes.failure import _handle_failure_impl as handle_failure  # noqa: E402
from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


def _sub(sid, deps=None):
    return SubTask(
        id=sid, description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[]), depends_on=deps or [],
    )


def _out(sid, l1=True):
    return WorkerOutput(subtask_id=sid, diff="x", summary="", confidence=Confidence.HIGH, l1_passed=l1)


# ── D24(a)：契约失败 retry 分支 ──
def test_contract_retry_requeues_and_clears():
    state = {
        "verification_failure": "contract",
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _out("st-1"), "st-2": _out("st-2")},
        "dispatch_remaining": [],
        "subtask_retry_counts": {},
    }
    out = asyncio.run(handle_failure(state))
    assert out.get("failure_strategy") == "retry"
    # 相关失败子任务从结果中 pop（下轮真重跑），成功兄弟保留
    assert "st-2" not in out["subtask_results"], "契约失败子任务应被 pop 以真重派"
    assert "st-1" in out["subtask_results"], "成功兄弟不应被清"
    # 加回 dispatch_remaining
    assert "st-2" in out["dispatch_remaining"], "契约失败子任务应加回 dispatch_remaining"
    # failed 清空（下轮 monitor 走 dispatch 而非再进 handle_failure）
    assert out["failed_subtask_ids"] == []
    # 重试计数自增
    # 语义演进（阶段6 D13）：契约重试记独立表，不再挤兑 capability 配额
    assert out["contract_retry_counts"].get("st-2") == 1
    print("  ✅ 契约 retry：pop 结果 + 加回 remaining + 清 failed + 计数自增")


# ── D24(b)：dispatch 早退分支 always-emit failed_subtask_ids ──
def test_dispatch_early_return_backfills_failed_ids():
    # st-down 依赖 st-up；st-up 的结果 L1 未过 → completed_l1_ids 排除 → st-down 不 ready →
    # to_dispatch 空 → 早退。断言早退结果始终回填 failed_subtask_ids（不残留/不丢）。
    plan = TaskPlan(subtasks=[_sub("st-up"), _sub("st-down", deps=["st-up"])])
    state = {
        "plan": plan,
        "dispatch_remaining": ["st-down"],
        "subtask_results": {"st-up": _out("st-up", l1=False)},
        "failed_subtask_ids": ["st-up"],
    }
    out = asyncio.run(dispatch(state))
    assert "failed_subtask_ids" in out, "早退分支必须回填 failed_subtask_ids(always-emit)"
    assert out["failed_subtask_ids"] == ["st-up"], "应回填当前 state 的 failed 列表(不丢不残)"
    print("  ✅ dispatch 早退分支 always-emit failed_subtask_ids")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("D24 全部通过")
