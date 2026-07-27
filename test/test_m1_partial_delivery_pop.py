"""M-1（21 号文）治本回归：部分交付臂对称 pop + 放弃者产物绝不随交付。

死亡场景（治前）：merge 冲突型失败（delete-vs-modify）的子任务 L1 通过、结果滞留
subtask_results；部分交付臂是四条放弃臂中唯一不 pop 的 → 重跑 merge 冲突双双重进
→ 确定性重演 → 同态返回空转 → recursion_limit → PARTIAL 承诺变 FAILED。
治后：部分交付臂对称 pop 放弃者并回写 subtask_results；幸存者集合=交付面唯一来源。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.nodes as nodes  # noqa: E402
from swarm.types import (  # noqa: E402
    Complexity, FileScope, SubTask, SubTaskDifficulty, SubTaskModality,
    TaskPlan, WorkerOutput,
)


def _st(sid: str) -> SubTask:
    return SubTask(
        id=sid, description="d", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT, scope=FileScope(writable=[f"{sid}.py"]),
    )


def _run_arm():
    plan = TaskPlan(subtasks=[_st("st-ok"), _st("st-a"), _st("st-b")],
                    parallel_groups=[["st-ok"], ["st-a", "st-b"]])
    state = {
        "complexity": Complexity.MEDIUM,
        "plan": plan,
        "failed_subtask_ids": ["st-a", "st-b"],
        "subtask_results": {
            "st-ok": WorkerOutput(subtask_id="st-ok", diff="+ok", summary="x", l1_passed=True),
            # 冲突双子任务 L1 通过（merge 冲突型失败特征）——治前滞留进下轮 merge
            "st-a": WorkerOutput(subtask_id="st-a", diff="+a", summary="x", l1_passed=True),
            "st-b": WorkerOutput(subtask_id="st-b", diff="+b", summary="x", l1_passed=True),
        },
        "subtask_retry_counts": {"st-a": 5, "st-b": 5},  # deepest=5 > max_retries+1
        "subtask_alternate_ever_used": {"st-a": True, "st-b": True},  # 闸1 跳过
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    with patch.object(nodes, "_get_brain_llm", side_effect=RuntimeError("llm down")):
        return asyncio.run(nodes.handle_failure(state))


def test_partial_delivery_arm_pops_abandoned_results():
    out = _run_arm()
    assert out.get("failure_strategy") == "abandon", out.get("failure_strategy")
    sr = out.get("subtask_results")
    assert sr is not None, "M-1：部分交付臂必须对称回写 subtask_results"
    assert "st-a" not in sr and "st-b" not in sr, "放弃者结果必须 pop（绝不随交付）"
    assert "st-ok" in sr, "完成兄弟结果必须保留（PARTIAL 的交付面）"
    assert set(out.get("abandoned_subtask_ids") or []) >= {"st-a", "st-b"}
    print("  ✅ 部分交付臂对称 pop：放弃者出结果账、幸存者保留")


def test_abandoned_results_never_reenter_merge_input():
    """纵深语义核验：放弃者既在 abandoned 集、又不在结果账——重跑 merge 的输入
    （state.subtask_results）天然不含它们；MERGE 侧 M-1 滤是同集 backstop。"""
    out = _run_arm()
    sr = out.get("subtask_results") or {}
    ab = set(out.get("abandoned_subtask_ids") or [])
    assert not (set(sr) & ab), "结果账与放弃集必须互斥（空转环根源消除）"
    print("  ✅ 结果账 ∩ 放弃集 = ∅（重跑 merge 无冲突重演源）")


if __name__ == "__main__":
    test_partial_delivery_arm_pops_abandoned_results()
    test_abandoned_results_never_reenter_merge_input()
    print("\n=== M-1 部分交付臂对称 pop: 2/2 passed ===")
