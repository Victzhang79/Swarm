"""#29-8 M-6：auto_accept 对 merge_owner_drops 必须有闸（此前零消费）。

owner 裁决丢件的判据来自 plan 声明的写权——声明错时被丢的可能是真产出。
人工闸 payload 有账，但 auto_accept 路径不构造 payload → 丢件无人看见就放行。
同族 merge_rebase_dropped 已有闸（partial_delivery_ids），两档必须对称。
"""
from __future__ import annotations

from swarm.brain.gates import can_auto_accept_delivery

_BASE = {"l2_passed": True, "l3_passed": True, "subtask_results": {}, "plan": None}


def test_owner_drops_blocks_auto_accept():
    allow, reason = can_auto_accept_delivery({
        **_BASE,
        "merge_owner_drops": [
            {"file": "alarm-task/pom.xml", "owner": "st-1",
             "dropped": ["st-2"], "dropped_lines": 7},
        ],
    })
    assert allow is False, "有 owner 裁决丢件绝不自动放行（被丢的可能是真产出）"
    assert "merge_owner_drops" in reason
    assert "alarm-task/pom.xml" in reason


def test_no_owner_drops_unaffected():
    allow, _ = can_auto_accept_delivery(_BASE)
    assert allow is True


def test_empty_owner_drops_unaffected():
    """clean 路径无条件写 []（ACCOUNTING_KEY_LIFECYCLE round）——空账不拦。"""
    allow, _ = can_auto_accept_delivery({**_BASE, "merge_owner_drops": []})
    assert allow is True


def test_owner_unions_alone_do_not_block():
    """★与 H-1 联动★：并集成功=一行没丢，只记 unions 账——绝不冤触 M-6 闸。"""
    allow, _ = can_auto_accept_delivery({
        **_BASE,
        "merge_owner_unions": [
            {"file": "alarm-task/pom.xml", "owner": "st-1", "unioned": ["st-2"]},
        ],
    })
    assert allow is True, "并集成功不是丢件，绝不可拦 auto_accept"
