"""P0-A 回归：confirm 节点禁止 fan-out（静态边 + 条件边并存）拓扑守护。

根因（task 37460a5b）：brain/graph.py 自初始提交起，confirm 节点同时挂了
  - 无条件静态边  graph.add_edge("confirm", "dispatch")
  - 条件边        graph.add_conditional_edges("confirm", after_confirm, {...})
LangGraph 将同节点的静态边 + 条件边当 fan-out 并行触发：即使 after_confirm 返回
"end"(REJECT)，静态边仍把流程拽进 dispatch，导致"计划已 reject 却照样派发"。

本测试用图拓扑断言（而非仅测 after_confirm 返回值）守护不变量：
confirm 的出口必须【完全】由条件边决定，不得存在任何无条件后继边。
任何人将来再误加 add_edge("confirm", ...) 会立刻让本测试变红。
"""

from __future__ import annotations

from swarm.brain.graph import after_confirm, build_brain_graph
from swarm.types import HumanDecision


def test_confirm_has_no_unconditional_static_edge():
    """confirm 节点不得有任何无条件静态出边（fan-out 防护核心）。"""
    graph = build_brain_graph()
    # StateGraph.edges 是静态边集合 {(src, dst), ...}
    confirm_static_targets = {dst for (src, dst) in graph.edges if src == "confirm"}
    assert confirm_static_targets == set(), (
        f"confirm 出现无条件静态边 {confirm_static_targets}，会与 after_confirm 条件边"
        f" fan-out 并行触发，导致 REJECT 仍被派发。出口必须只由条件边决定。"
    )


def test_confirm_routes_only_via_conditional_branch():
    """confirm 的出口必须由 after_confirm 条件边提供（覆盖 dispatch/end/plan 三出口）。"""
    graph = build_brain_graph()
    assert "confirm" in graph.branches, "confirm 必须挂 after_confirm 条件边"
    branch_specs = graph.branches["confirm"]
    # 收集条件边声明的所有 ends 目标
    ends = set()
    for spec in branch_specs.values():
        ends.update((spec.ends or {}).values())
    # 期望三出口：dispatch / __end__ / plan（__end__ 是 END 的内部名）
    assert "dispatch" in ends and "plan" in ends, f"条件边出口不全: {ends}"
    assert any(e in ("__end__", "end") for e in ends), f"条件边缺少 END 出口: {ends}"


def test_after_confirm_reject_returns_end():
    """单元层：REJECT 必须路由到 end（条件边语义本身正确）。"""
    assert after_confirm({"human_decision": HumanDecision.REJECT}) == "end"
    assert after_confirm({"human_decision": HumanDecision.ACCEPT}) == "dispatch"
    # 其它（REVISE/None）→ plan
    assert after_confirm({"human_decision": HumanDecision.REVISE}) == "plan"


if __name__ == "__main__":
    test_confirm_has_no_unconditional_static_edge()
    print("  ✅ confirm 无无条件静态边")
    test_confirm_routes_only_via_conditional_branch()
    print("  ✅ confirm 出口由条件边覆盖 dispatch/end/plan")
    test_after_confirm_reject_returns_end()
    print("  ✅ after_confirm REJECT→end / ACCEPT→dispatch / REVISE→plan")
    print("\n=== P0-A confirm fan-out 拓扑守护: 3/3 passed ===")
