"""P1 + Q4 回归：DELIVER/CONFIRM auto_accept 放行闸门（brain.gates 单一事实源）。

根因（task 37460a5b）：DELIVER 的 auto_accept 无条件 ACCEPT，escalate 的失败任务被
当成功放行 → after_deliver 路由 LEARN_SUCCESS → 失败被学成成功模式污染知识库。
CONFIRM 已在 P0-3 修过同构问题，DELIVER 漏修。本次收敛到 brain.gates 统一判定。

测两层：
  1. gate 纯函数语义（含 l3 三态边界）。
  2. deliver 节点在各种失败态下 auto_accept → REJECT；成功态 → ACCEPT。
"""

from __future__ import annotations

import swarm.brain.nodes as nodes
from swarm.brain.gates import can_auto_accept_delivery, can_auto_accept_plan
from swarm.types import HumanDecision


# ─────────────── gate 纯函数语义 ───────────────

def test_gate_delivery_success_path():
    """全部通过 → 放行。"""
    allow, reason = can_auto_accept_delivery(
        {"l2_passed": True, "l3_passed": True, "failed_subtask_ids": [], "failure_escalated": False}
    )
    assert allow is True, reason


def test_gate_delivery_l3_skipped_is_not_failure():
    """l3_passed=None（跳过）不得误判为失败——否则关闭 L3 的项目永远无法 auto_accept。"""
    allow, reason = can_auto_accept_delivery(
        {"l2_passed": True, "l3_passed": None, "failed_subtask_ids": [], "failure_escalated": False}
    )
    assert allow is True, reason


def test_gate_delivery_l3_explicit_false_blocks():
    allow, reason = can_auto_accept_delivery(
        {"l2_passed": True, "l3_passed": False, "failed_subtask_ids": [], "failure_escalated": False}
    )
    assert allow is False and "l3" in reason


def test_gate_delivery_escalated_blocks():
    allow, reason = can_auto_accept_delivery(
        {"l2_passed": True, "l3_passed": True, "failure_escalated": True}
    )
    assert allow is False and "escalat" in reason


def test_gate_delivery_failed_subtasks_block():
    allow, reason = can_auto_accept_delivery(
        {"l2_passed": True, "l3_passed": True, "failed_subtask_ids": ["st-1-1"]}
    )
    assert allow is False and "failed" in reason


def test_gate_delivery_l2_fail_blocks():
    allow, reason = can_auto_accept_delivery({"l2_passed": False})
    assert allow is False and "l2" in reason


def test_gate_delivery_verification_failure_blocks():
    allow, reason = can_auto_accept_delivery(
        {"l2_passed": True, "l3_passed": True, "verification_failure": "merge_conflict"}
    )
    assert allow is False and "verification_failure" in reason


def test_gate_clarify_block_attributed_correctly_not_l2(  ):
    """治本 661ecacb：虚假前提阻断（从未跑 L2）应归因 clarification_required，绝不误报 l2_failed。"""
    allow, reason = can_auto_accept_delivery({
        "clarify_blocked_by_facts": True,
        "clarify_summary": "需求存在虚假前提：渠道配置表列出的 4 种渠道已覆盖 PRD 全部发送方式",
        # 注意：l2_passed 缺省 False（L2 从未跑），旧逻辑会误报 l2_failed
    })
    assert allow is False
    assert "clarification_required" in reason, reason
    assert "l2_failed" not in reason, "绝不能把'需澄清'误报成 L2 失败"
    assert "4 种渠道" in reason, "应带上具体虚假前提供用户澄清"


def test_gate_clarify_block_precedes_l2_check():
    """clarify 阻断优先级高于 l2：即便 l2_passed=False 也按 clarify 归因。"""
    allow, reason = can_auto_accept_delivery({
        "clarify_blocked_by_facts": True,
        "clarify_summary": "X",
        "l2_passed": False,
    })
    assert allow is False and "clarification_required" in reason


def test_gate_plan_invalid_blocks():
    allow, reason = can_auto_accept_plan({"plan_valid": False, "plan_validation_issues": ["悬空依赖"]})
    assert allow is False and "plan_invalid" in reason


def test_gate_plan_valid_passes():
    allow, _ = can_auto_accept_plan({"plan_valid": True})
    assert allow is True


# ─────────────── deliver 节点行为 ───────────────

def _deliver(state: dict):
    base = {"auto_accept": True, "task_id": "t", "merged_diff": "x"}
    base.update(state)
    return nodes.deliver(base)


def test_deliver_auto_accept_rejects_escalated():
    """task 37460a5b 复现：escalate 的失败任务 auto_accept 必须 REJECT，不得 ACCEPT。"""
    out = _deliver({"failure_escalated": True, "l2_passed": False, "failed_subtask_ids": ["st-1-1"]})
    assert out["human_decision"] == HumanDecision.REJECT, out
    assert "deliver_auto_reject_reason" in out


def test_deliver_auto_accept_passes_real_success():
    out = _deliver({"failure_escalated": False, "l2_passed": True, "l3_passed": True, "failed_subtask_ids": []})
    assert out["human_decision"] == HumanDecision.ACCEPT, out


def test_after_deliver_reject_routes_learn_failure():
    """REJECT → learn_failure（失败学成错误模式，不污染成功知识库）。"""
    from swarm.brain.graph import after_deliver
    assert after_deliver({"human_decision": HumanDecision.REJECT}) == "learn_failure"
    assert after_deliver({"human_decision": HumanDecision.ACCEPT}) == "learn_success"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== P1+Q4 auto_accept 放行闸门: {len(fns)}/{len(fns)} passed ===")
