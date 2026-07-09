"""P2 回归：瞬时(transient)失败退避重试，与 capability 配额隔离。

根因（task 37460a5b）：Connection error/Internal Server Error 等基础设施抖动，过去与
拒答/空 diff 等能力问题混在同一条 retry 阶梯共享 subtask_retry_counts。st-1-1 在
02:01-02:02 连撞两次 Connection error（各 0.8s，零退避），烧光配额直接 escalate。

修复：
  1. classify_failure 区分 transient / capability。
  2. worker 异常路径把 failure_class 写进 l1_details。
  3. handle_failure：本批全为 transient → 带指数退避的轻量重试(独立计数器，上限 3)，
     不消耗 capability 配额；混入 capability 则交给换模型阶梯。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.models.errors import (
    CAPABILITY,
    TRANSIENT,
    backoff_seconds,
    classify_failure,
)
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


# ─────────────── classify_failure 语义 ───────────────

def test_classify_connection_error_is_transient():
    import openai
    exc = openai.APIConnectionError(request=None)  # type: ignore[arg-type]
    assert classify_failure(exc) == TRANSIENT


def test_classify_text_connection_error_transient():
    assert classify_failure("Connection error.") == TRANSIENT
    assert classify_failure("Internal Server Error") == TRANSIENT
    assert classify_failure("503 Service Unavailable") == TRANSIENT


def test_classify_refusal_is_capability():
    assert classify_failure("Sorry, need more steps to process this request.") == CAPABILITY


def test_classify_empty_diff_is_capability():
    assert classify_failure("empty_diff_but_changes_expected") == CAPABILITY


def test_classify_refusal_with_timeout_word_still_capability():
    """拒答标记必须优先于 transient 关键词，避免 'timeout' 等词误伤。"""
    assert classify_failure("I cannot do this, request timed out maybe") == CAPABILITY


def test_classify_unknown_is_none():
    assert classify_failure("some random failure") is None


def test_backoff_exponential_capped():
    assert backoff_seconds(1) == 2.0
    assert backoff_seconds(2) == 4.0
    assert backoff_seconds(3) == 8.0
    assert backoff_seconds(4) == 8.0  # capped


# ─────────────── handle_failure 分流 ───────────────

def _plan():
    return TaskPlan(
        subtasks=[
            SubTask(
                id="st-1-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                modality=SubTaskModality.TEXT, scope=FileScope(writable=["a.py"]), intent="modify",
            )
        ],
        parallel_groups=[["st-1-1"]],
    )


def _run_handle(state):
    base = {
        "complexity": Complexity.MEDIUM,
        "plan": _plan(),
        "failed_subtask_ids": ["st-1-1"],
        "subtask_retry_counts": {},
        "subtask_transient_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    base.update(state)
    # patch 掉 LLM 决策器，避免真实网络；退避 sleep 也 patch 成瞬时
    with patch.object(nodes, "_get_brain_llm", side_effect=RuntimeError("no llm")), \
         patch.object(asyncio, "sleep", return_value=None):
        return asyncio.run(nodes.handle_failure(base))


def test_transient_failure_uses_backoff_not_capability_quota():
    """task 37460a5b 复现：connection error → 走 transient 退避，不消耗 capability 配额。"""
    out = WorkerOutput(
        subtask_id="st-1-1", diff="", summary="执行异常: Connection error.",
        l1_passed=False, l1_details={"error": "Connection error.", "failure_class": TRANSIENT},
    )
    res = _run_handle({"subtask_results": {"st-1-1": out}})
    assert res["failure_strategy"] == "retry"
    # 语义演进（阶段3.9 H-F7）：全局 bool → 按子任务映射；意图不变=transient 不换模型
    assert not res.get("subtask_use_alternate", {}).get("st-1-1")
    # transient 计数 +1，capability 配额未动
    assert res["subtask_transient_counts"]["st-1-1"] == 1
    assert "subtask_retry_counts" not in res or res.get("subtask_retry_counts", {}).get("st-1-1", 0) == 0


def test_transient_retry_exhausted_falls_to_capability_ladder():
    """transient 退避用尽(>3) → 转入 capability 阶梯（基础设施持续不可用）。"""
    out = WorkerOutput(
        subtask_id="st-1-1", diff="", summary="执行异常: Connection error.",
        l1_passed=False, l1_details={"failure_class": TRANSIENT},
    )
    res = _run_handle({
        "subtask_results": {"st-1-1": out},
        "subtask_transient_counts": {"st-1-1": 3},  # 已退避 3 次
    })
    # 落入 capability 阶梯：消费 subtask_retry_counts
    assert "subtask_retry_counts" in res
    assert res["subtask_retry_counts"].get("st-1-1", 0) >= 1


def test_capability_failure_uses_retry_ladder_not_backoff():
    """拒答(capability) → 走换模型阶梯，消费 capability 配额，不走 transient 退避。"""
    out = WorkerOutput(
        subtask_id="st-1-1", diff="", summary="Sorry, need more steps to process this request.",
        l1_passed=False, l1_details={"failure_class": CAPABILITY},
    )
    res = _run_handle({"subtask_results": {"st-1-1": out}})
    assert "subtask_retry_counts" in res
    assert res["subtask_retry_counts"].get("st-1-1", 0) >= 1
    # transient 配额未动
    assert res.get("subtask_transient_counts", {}).get("st-1-1", 0) == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== P2 transient 分类+退避+配额隔离: {len(fns)}/{len(fns)} passed ===")
