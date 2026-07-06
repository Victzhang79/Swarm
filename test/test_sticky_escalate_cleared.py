"""批4c（CODEWALK 根因A 粘滞路由键族·专项取证 CONFIRMED-BUG）：failure_escalated 粘滞。

全仓只有 10 处写 True、0 处清零（LangGraph last-write-wins 永驻）。后果：
escalate→人工 REVISE→修订/replan 成功后，gates.py:112 因残留 True 永拒 auto_accept
（人工白干一轮）；escalate→REVISE→再失败再 escalate 时 after_merge:285 残留条件把
干净合并再送 DELIVER 且污染学习标记。round27 merge_conflicts 粘滞同族
（"仅条件写无人清"模式第三例）。

修法（取证报告方案 A+B）：revision()=重新开始清零；handle_failure/confirm 的所有
【非 escalate】决策返回都显式清零（escalate 分支按需重新置 True，A6 路由不受影响）。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


_DIFF_A = """--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
 x = 1
+a = 1
"""


class _Resp:
    def __init__(self, content):
        self.content = content


def _fake_llm(payload: str):
    class _L:
        async def ainvoke(self, _msgs):
            return _Resp(payload)
    return lambda: _L()


def _plan():
    return TaskPlan(
        subtasks=[SubTask(id="st-1", description="d",
                          difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                          scope=FileScope(writable=["a.py"]))],
        parallel_groups=[["st-1"]],
    )


def _failed_state(**over):
    s = {
        "complexity": Complexity.MEDIUM,
        "plan": _plan(),
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": WorkerOutput(subtask_id="st-1", diff="", summary="x",
                                                 l1_passed=False)},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
        # 上一轮 escalate 的粘滞残留
        "failure_escalated": True,
        "failure_strategy": "escalate",
    }
    s.update(over)
    return s


def test_handle_failure_replan_clears_stale_escalated():
    payload = json.dumps({"strategy": "replan", "reasoning": "拆分不合理"}, ensure_ascii=False)
    with patch.object(nodes, "_get_brain_llm", _fake_llm(payload)):
        out = asyncio.run(nodes.handle_failure(_failed_state()))
    assert out.get("failure_strategy") == "replan"
    assert out.get("failure_escalated") is False, \
        "非 escalate 决策必须清历史粘滞标记（否则 gates 永拒 auto_accept）"


def test_handle_failure_retry_clears_stale_escalated():
    payload = json.dumps({"strategy": "retry", "reasoning": "瞬时"}, ensure_ascii=False)
    with patch.object(nodes, "_get_brain_llm", _fake_llm(payload)):
        out = asyncio.run(nodes.handle_failure(_failed_state()))
    assert out.get("failure_strategy") in ("retry", "retry_alternate")
    assert out.get("failure_escalated") is False


def test_revision_clears_stale_escalated():
    payload = json.dumps({"revision_subtasks": [{
        "id": "rev-1", "description": "修复按钮", "difficulty": "medium",
        "scope": {"writable": ["a.py"]},
    }]}, ensure_ascii=False)
    state = _failed_state(
        revision_feedback="按钮没反应", merged_diff="", task_description="做个页面",
        failed_subtask_ids=[],
    )
    with patch.object(nodes, "_get_brain_llm", _fake_llm(payload)):
        out = asyncio.run(nodes.revision(state))
    assert out.get("failure_escalated") is False, \
        "修订=重新开始，必须清历史 escalate 粘滞"


def test_plan_simple_path_clears_stale_escalated():
    """R2-1：PLAN 是新一轮规划起点，须无条件清历史 escalate 粘滞——堵"首次 REVISE→PLAN
    （无 old_results，_surgical_replan_reset 返回空）"漏清线。SIMPLE 快速路径无 LLM 依赖。"""
    state = {
        "task_description": "改一行配置",
        "complexity": Complexity.SIMPLE,
        "affected_files": ["conf.py"],
        "failure_escalated": True,  # 上一轮残留
        "failure_strategy": "escalate",
    }
    out = asyncio.run(nodes.plan(state))
    assert out.get("failure_escalated") is False


def test_after_merge_routing_with_stale_strategy_residue():
    """R2-1 图级：merge 干净轮清零后，即便 failure_strategy 仍残留 "escalate"，
    after_merge 也必须路由 VERIFY_L2 而非把干净合并送 DELIVER（A6 条件需两键同真）。"""
    from swarm.brain.graph import after_merge

    state = {
        "failure_escalated": True,
        "failure_strategy": "escalate",
        "merge_conflicts": [],
        "failed_subtask_ids": [],
        "rebase_subtask_ids": [],
        "subtask_results": {
            "st-a": WorkerOutput(subtask_id="st-a", diff=_DIFF_A, summary="", l1_passed=True),
        },
    }
    out = nodes.merge(state)
    state.update(out)
    assert state.get("failure_escalated") is False
    assert after_merge(state) == "verify_l2", \
        "干净合并 + 残留 strategy=escalate 不得被 after_merge 送 DELIVER"


def test_gates_semantics_around_escalated_flag():
    """闸门语义对照：残留 True 拒绝（这正是粘滞的伤害面），清零后同一状态放行。"""
    from swarm.brain.gates import can_auto_accept_delivery

    base = {"l2_passed": True, "l3_passed": True, "failed_subtask_ids": []}
    ok_stale, reason_stale = can_auto_accept_delivery({**base, "failure_escalated": True})
    assert ok_stale is False and "failure_escalated" in reason_stale
    ok_clean, _ = can_auto_accept_delivery({**base, "failure_escalated": False})
    assert ok_clean is True