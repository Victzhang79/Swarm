#!/usr/bin/env python3
"""Wave 2 编排 CRITICAL 反向测试（TD2606-A5/A6/A8；A7 见 test_memory_architecture/waved）。

- A5：规划 LLM 失败的空 scope 假计划 → can_auto_accept_plan fail-fast 拦下（不静默 dispatch）。
- A6：merge rebase 超限 escalate → after_merge 路由 DELIVER（不丢信号、不死循环）。
- A8：路由档整条链不可达 → validate_routing_reachability 报 error（死模型不静默）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_a5_plan_generation_failed_blocks_auto_accept():
    from swarm.brain.gates import can_auto_accept_plan

    allow, reason = can_auto_accept_plan({"plan_generation_failed": True, "plan_valid": True})
    assert allow is False
    assert "plan_generation_failed" in reason
    # 对照：正常计划放行
    allow_ok, _ = can_auto_accept_plan({"plan_valid": True})
    assert allow_ok is True
    print("  ✅ A5：规划失败假计划被 auto_accept fail-fast 拦下")


def test_a6_merge_escalate_routes_to_deliver():
    from swarm.brain.graph import after_merge

    # rebase 超限 escalate（merge 节点设的状态）→ 必须路由 deliver，不落 verify_l2 死循环
    route = after_merge({"failure_escalated": True, "failure_strategy": "escalate",
                         "rebase_subtask_ids": [], "merge_conflicts": []})
    assert route == "deliver", f"escalate 应路由 deliver, got {route}"
    # 对照：无 escalate、无冲突、无 rebase → verify_l2
    assert after_merge({"rebase_subtask_ids": [], "merge_conflicts": []}) == "verify_l2"
    # 对照：有冲突 → handle_failure
    assert after_merge({"merge_conflicts": [{"file_path": "x"}]}) == "handle_failure"
    print("  ✅ A6：merge escalate 路由 deliver（不丢信号/不死循环）")


def test_a8_routing_reachability_flags_dead_chain(monkeypatch=None):
    import swarm.models.capability_store as cap
    from swarm.models.router import ModelRouter

    router = ModelRouter()

    # 能力库为空 → 不离线误报
    orig = cap.list_capabilities
    cap.list_capabilities = lambda *a, **k: []
    try:
        assert router.validate_routing_reachability() == [], "能力库为空不应误报"
        # 能力库已探测但【不含】任何路由模型 → 各档整条链不可达 → error
        cap.list_capabilities = lambda *a, **k: [{"model_id": "some-unrelated-model"}]
        issues = router.validate_routing_reachability()
        assert issues, "已探测库不含路由模型应报不可达"
        assert any(i["severity"] == "error" and i["kind"] == "whole_chain_unreachable" for i in issues)
    finally:
        cap.list_capabilities = orig
    print("  ✅ A8：整条链不可达报 error，能力库空不误报")


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {fn.__name__}: {e}")
            fails += 1
    sys.exit(1 if fails else 0)
