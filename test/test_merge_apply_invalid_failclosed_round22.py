#!/usr/bin/env python3
"""#1(a) round22：MERGE apply 失败必须 fail-closed 阻断交付（不静默放行成功）。

根因：nodes/__init__.py:3124-3140 当 clean-merge 的 merged_diff `git apply --check` 失败
（确定性组装缺陷）时，只 logger.error+dump，**不设任何失败态** → after_merge 默认 → VERIFY_L2，
而 VERIFY_L2 复核 `if project_path and merged_diff`（verify.py:76）在 project_path 空时整块跳过
→ 非法 patch 可能不被拦 → 假绿放行。

治本：apply-invalid 时设 failure_escalated + failure_strategy=escalate + l2_passed=False +
verification_failure=merge_apply_invalid，复用既有 escalate 路径（after_merge:285 → DELIVER，
人工审核）。本测试锁定"这些信号 → 路由到 deliver + 交付 gate 拒绝放行"的 fail-closed 契约。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.graph import after_merge  # noqa: E402
from swarm.brain.gates import can_auto_accept_delivery  # noqa: E402


# merge 节点在 apply-invalid 时应写入的信号（治本后）
_APPLY_INVALID_STATE = {
    "failure_escalated": True,
    "failure_strategy": "escalate",
    "l2_passed": False,
    "verification_failure": "merge_apply_invalid",
    "merged_diff": "diff --git a/x b/x\n@@ bad hunk @@\n",
    "merge_conflicts": [],
    "rebase_subtask_ids": [],
}


def test_apply_invalid_routes_to_deliver_escalate():
    route = after_merge(_APPLY_INVALID_STATE)
    assert route == "deliver", "apply-invalid(escalate) 必须走 DELIVER 人工审核，不得默认进 VERIFY_L2 假绿"
    print("  ✅ apply-invalid → DELIVER(escalate)")


def test_apply_invalid_delivery_gate_blocks():
    allow, reason = can_auto_accept_delivery(_APPLY_INVALID_STATE)
    assert allow is False, "apply-invalid 绝不能被交付 gate 当成功放行（fail-closed）"
    # 归因命中 failure_escalated / l2_failed / verification_failure 任一即可
    assert reason, "拒绝必须带归因"
    print(f"  ✅ 交付 gate 拒绝放行：{reason[:40]}")


def test_clean_apply_ok_still_reaches_verify_l2():
    """不回归：apply_ok 的干净合并（无 escalate 信号）仍正常进 VERIFY_L2。"""
    ok_state = {"merge_conflicts": [], "rebase_subtask_ids": [], "merged_diff": "diff"}
    assert after_merge(ok_state) == "verify_l2"
    print("  ✅ 干净合并 → VERIFY_L2（不回归）")


if __name__ == "__main__":
    test_apply_invalid_routes_to_deliver_escalate()
    test_apply_invalid_delivery_gate_blocks()
    test_clean_apply_ok_still_reaches_verify_l2()
    print("\n✅ #1(a) MERGE apply 失败 fail-closed 全部通过")
