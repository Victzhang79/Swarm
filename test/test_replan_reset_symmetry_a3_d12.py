"""A3+D12（2026-07-09 深读登记册·阶段0）：replan 出口重置对称性 — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §二 A3 / §五 D12：
  - failure.py 有三个 replan 出口：runtime_smoke 归因不出(:278)、L2 全量(:353)、
    能力分析(:947)。round36 #9 给 L2 出口补了 plan_retry_count=0 +
    plan_validation_feedback=""（新规划目标须给全新校验预算、旧覆盖 issue 不得污染
    新规划），能力出口只补了 plan_retry_count——runtime_smoke 出口两者全漏。
    后果：runtime replan 继承覆盖重试已耗尽的 plan_retry_count → 新计划首次校验
    失败即 3/3 耗尽 → CONFIRM→REJECT 整任务死。
  - D12：l2_targeted 由 verify.py _l2_failure_state 条件写（归因出才 True），
    定向恢复分支消费后清（:345），但全量 replan(:353) 与熔断 escalate(:298) 出口
    不清 → 下一轮 L2 失败若归因不出（不 emit 该键），粘滞 True 会把全员连坐误判成
    "已归因定向"，走错恢复路径。
  - 同类 sibling：能力 replan 出口(:947)漏清 plan_validation_feedback（与 L2 出口
    :365 注释同一理由——旧覆盖 issue 污染新规划）。

栈无关：全部用抽象子任务，无任何语言/框架/领域词汇。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


def _st(sid, writable=None):
    return SubTask(
        id=sid,
        description=f"subtask {sid}",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=list(writable or ["a"])),
    )


def _plan(*subtasks):
    return TaskPlan(subtasks=list(subtasks),
                    parallel_groups=[[s.id] for s in subtasks])


def _wo(sid, l1=True):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if l1 else "",
        summary="",
        l1_passed=l1,
        l1_details={},
    )


class _FakeResp:
    def __init__(self, content):
        self.content = content


def _fake_llm_returning(strategy, reasoning="结构性拆分错误"):
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp('{"strategy":"%s","reasoning":"%s"}' % (strategy, reasoning))
    return lambda: _L()


def _run(state):
    return asyncio.run(nodes.handle_failure(state))


# ─────────────────── A3：runtime_smoke replan 出口重置对称 ───────────────────

def test_runtime_smoke_replan_resets_plan_retry_budget_and_feedback():
    """runtime 归因不出 → replan 出口必须与 L2/能力出口对称：清 plan_retry_count 与
    plan_validation_feedback。否则新规划继承已耗尽的校验预算，首败即 CONFIRM REJECT。"""
    out = _run({
        "verification_failure": "runtime_smoke",
        "runtime_smoke_details": {"classification": "code_error"},  # 无文件证据→归因不出
        "plan": _plan(_st("st-1")),
        "failed_subtask_ids": [],
        "subtask_results": {"st-1": _wo("st-1")},
        "replan_count": 0,
        "plan_retry_count": 2,               # 早前覆盖重试已耗到 2
        "plan_validation_feedback": "- 未覆盖: req-stale9999",  # 旧覆盖 issue
    })
    assert out["failure_strategy"] == "replan"
    assert out.get("plan_retry_count") == 0, (
        "runtime replan 是新规划目标，必须给全新 plan 校验重试预算（round36 #9 同理）")
    assert out.get("plan_validation_feedback") == "", (
        "旧覆盖 issue 不得污染 runtime replan 的新规划")


# ─────────────────── D12：l2_targeted 出口清空 ───────────────────

def test_l2_blanket_replan_clears_l2_targeted():
    """l2_targeted=True 但无成功兄弟 → 落全量 replan 出口，必须清 l2_targeted——
    否则下一轮 L2 归因不出（不 emit 该键）时粘滞 True 把全员连坐误判成定向。"""
    out = _run({
        "verification_failure": "l2",
        "l2_targeted": True,
        "failed_subtask_ids": ["st-1", "st-2"],
        "subtask_results": {"st-1": _wo("st-1", l1=False), "st-2": _wo("st-2", l1=False)},
        "dispatch_remaining": [],
        "replan_count": 0,
    })
    assert out["failure_strategy"] == "replan"
    assert out.get("l2_targeted") is False, "全量 replan 出口必须清 l2_targeted 粘滞"


def test_l2_escalate_at_limit_clears_l2_targeted():
    """L2 replan 熔断升级人工的出口同样清 l2_targeted（出口对称，人工放行续跑不带脏标记）。"""
    out = _run({
        "verification_failure": "l2",
        "l2_targeted": True,
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": _wo("st-1", l1=False)},
        "dispatch_remaining": [],
        "replan_count": 99,
    })
    assert out["failure_strategy"] == "escalate"
    assert out.get("l2_targeted") is False, "escalate 出口必须清 l2_targeted 粘滞"


# ─────────────────── sibling：能力 replan 出口清 plan_validation_feedback ───────────────────

def test_capability_replan_clears_stale_validation_feedback():
    """能力分析 replan 出口已清 plan_retry_count（round36 #9），但漏清
    plan_validation_feedback——与 L2 出口注释同一理由：旧覆盖 issue 不得污染新规划。"""
    with patch.object(nodes, "_get_brain_llm", _fake_llm_returning("replan")):
        out = _run({
            "complexity": Complexity.MEDIUM,
            "plan": _plan(_st("st-1")),
            "failed_subtask_ids": ["st-1"],
            "subtask_results": {"st-1": _wo("st-1", l1=False)},
            "subtask_retry_counts": {},
            "dispatch_remaining": [],
            "degraded_reasons": [],
            "replan_count": 0,
            "plan_validation_feedback": "- 未覆盖: req-stale9999",
        })
    assert out.get("failure_strategy") == "replan", out.get("failure_strategy")
    assert out.get("plan_retry_count") == 0
    assert out.get("plan_validation_feedback") == "", (
        "能力 replan 出口必须与 L2 出口对称清旧覆盖 issue")
