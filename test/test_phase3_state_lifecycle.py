"""阶段3.8（登记册 §八 阶段3）：BrainState 记账键生命周期收敛。

取证=全键写点×清点矩阵（39 键），坐实 7 处粘滞/死键；治法=生命周期登记表
（state.ACCOUNTING_KEY_LIFECYCLE 单一事实源）+ 逐处补清 + 本文件行为锁：
  T1 use_alternate_model：handle_failure 仅 retry 两出口对称写，其余出口不发键
     → 粘滞 True 劫持全局模型路由。修=dispatch 消费点收口（本轮消费即清）。
  T2 adversarial_verify_round：收敛不归零→跨战役累计撞 MAX_ROUNDS→degraded 放行
     跳过复核=验证洞。修=全 NICE 收敛归零。
  T3/T4 subtask_transient_counts / subtask_force_strong：缺席 D08 签名剪枝
     → replan 后旧账饿死新子任务/永久强模型。修=进 _surgical_replan_reset。
  T5 runtime_smoke_last_signature：冒烟通过不清→跨"失败→修好→再失败"误判 plateau。
     修=pass 分支清空。
  T6 confirm_reason/deliver_auto_reject_reason：REVISE 后陈旧值污染终态归因。
     修=revision 清空（含 use_alternate/transient/force_strong 对称重置）。
  T7 targeted_recovery：写后全仓零读点零清点=死键。修=删除（4 写点+声明）。
"""

from __future__ import annotations

import asyncio
import typing
from unittest.mock import patch

import pytest

from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState
from swarm.types import (
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


def _sub(sid, desc="do", writable=("a.x",)):
    return SubTask(id=sid, description=desc, difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=list(writable), readable=[]), depends_on=[])


def _wo(sid, l1=True):
    return WorkerOutput(subtask_id=sid, diff="+x\n", summary="", l1_passed=l1,
                        confidence=Confidence.HIGH)


# ─────────────── 登记表：单一事实源 ───────────────

def test_registry_keys_all_declared_and_dead_key_removed():
    hints = typing.get_type_hints(BrainState, include_extras=True)
    missing = [k for k in ACCOUNTING_KEY_LIFECYCLE if k not in hints]
    assert not missing, f"登记键必须在 BrainState 声明（未声明=静默丢弃）: {missing}"
    assert set(ACCOUNTING_KEY_LIFECYCLE.values()) <= {
        "oneshot", "round", "monotonic", "terminal"}
    assert "targeted_recovery" not in hints, "死键（全仓零读点）已删，不得复活"


def test_registry_covers_new_stage3_keys():
    # 语义演进（阶段3.9 H-F7/H-F5）：use_alternate_model → subtask_use_alternate（按子任务）；
    # 新增 coverage_gap_residual（A6 残差 last-write-wins）。
    for k in ("coverage_watermark", "plan_soft_review_sig", "subtask_use_alternate",
              "coverage_gap_residual", "subtask_transient_counts", "subtask_force_strong",
              "adversarial_verify_round", "runtime_smoke_last_signature"):
        assert k in ACCOUNTING_KEY_LIFECYCLE, f"记账键 {k} 必须登记生命周期"


# ─────────────── T1：alternate 标记按子任务消费（3.9 H-F7 语义演进）───────────────
# 3.8 原修法=全局 bool 消费即清；3.9 复核坐实"失败撮被降优先级错开到后续批"时消费即清
# 会把路由送错人 → 升级为按子任务映射。意图不变：派出即清（不粘滞劫持路由）。

async def test_dispatch_consumes_alternate_flag():
    plan = TaskPlan(subtasks=[_sub("st-1")], parallel_groups=[["st-1"]])

    async def fake_worker(subtask, knowledge_context, project_id="", task_id="", **kw):
        return _wo(subtask.id)

    state = {
        "task_id": "t1", "project_id": "p1", "plan": plan,
        "subtask_results": {}, "dispatch_remaining": ["st-1"],
        "failed_subtask_ids": [], "knowledge_context": {},
        "subtask_use_alternate": {"st-1": True},  # 上一轮 retry_alternate 决策
    }
    from swarm.brain.nodes.dispatch import dispatch
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        out = await dispatch(state)
    assert out.get("subtask_use_alternate") == {}, (
        "派出的子任务标记必须消费即清——否则单次 alternate 决策劫持后续轮的模型路由")


# ─────────────── T2：adversarial round 收敛归零 ───────────────

def test_adversarial_round_resets_on_convergence(monkeypatch):
    import swarm.brain.nodes as nodes

    class _PassReviewer:
        model_name = "m"

        async def ainvoke(self, messages):
            class R:
                content = ('{"reviews": [{"subtask_id": "st-1", "verdict": "PASS",'
                           ' "issue": "", "failure_scenario": ""}]}')
            return R()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _PassReviewer())
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: _PassReviewer())
    from swarm.brain.nodes.adversarial import adversarial_verify
    st = {
        "complexity": "complex",
        "plan": TaskPlan(subtasks=[_sub("st-1")], parallel_groups=[["st-1"]]),
        "subtask_results": {"st-1": _wo("st-1")},
        "dispatch_remaining": [], "failed_subtask_ids": [],
        "adversarial_verify_round": 1,  # 前一次战役累计（=2 时即撞 MAX_ROUNDS 短路，正是病理）
    }
    out = asyncio.run(adversarial_verify(st))
    assert out["adversarial_verify_passed"] is True
    assert out["adversarial_verify_round"] == 0, (
        "收敛必须归零——跨战役累计会让后期失败批进场即撞 MAX_ROUNDS 短路复核（验证洞）")


# ─────────────── T3/T4：D08 剪枝扩到瞬时配额与强模型标记 ───────────────

def test_surgical_reset_prunes_transient_and_force_strong():
    from swarm.brain.nodes import _surgical_replan_reset
    old_plan = TaskPlan(subtasks=[_sub("st-1", desc="旧语义", writable=("a.x",))])
    new_plan = TaskPlan(subtasks=[_sub("st-1", desc="全新语义", writable=("b.y",))])
    out = _surgical_replan_reset(
        {"st-1": _wo("st-1")}, old_plan, new_plan,
        old_transient_counts={"st-1": 3}, old_force_strong={"st-1": True})
    assert out["subtask_transient_counts"] == {}, (
        "签名变=语义新子任务，旧瞬时配额粘滞会提前判耗尽 escalate（与五张兄弟表同纪律）")
    assert out["subtask_force_strong"] == {}, "旧强模型标记粘滞=成本劫持"


def test_surgical_reset_preserves_when_signature_unchanged():
    from swarm.brain.nodes import _surgical_replan_reset
    plan = TaskPlan(subtasks=[_sub("st-1")])
    out = _surgical_replan_reset(
        {"st-1": _wo("st-1")}, plan, plan,
        old_transient_counts={"st-1": 2}, old_force_strong={"st-1": True})
    assert out["subtask_transient_counts"] == {"st-1": 2}
    assert out["subtask_force_strong"] == {"st-1": True}


# ─────────────── T6：revision 对称重置 ───────────────

async def test_revision_clears_lifecycle_keys(monkeypatch):
    import swarm.brain.nodes as nodes

    class _RevLLM:
        async def ainvoke(self, messages):
            class R:
                content = ('{"revision_subtasks": [{"id": "rev-1", "description": "按反馈修改",'
                           ' "scope": {"writable": ["a.x"], "readable": []}}]}')
            return R()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _RevLLM())
    out = await nodes.revision({
        "revision_feedback": "改一下", "merged_diff": "", "task_description": "t",
        "plan": TaskPlan(subtasks=[_sub("st-1")], parallel_groups=[["st-1"]]),
        "subtask_results": {"st-1": _wo("st-1")}, "project_id": "",
    })
    # 语义演进（3.9 H-F7）：use_alternate_model→subtask_use_alternate，重置值 False→{}
    for k, v in (("subtask_transient_counts", {}), ("subtask_force_strong", {}),
                 ("subtask_use_alternate", {}), ("confirm_reason", ""),
                 ("deliver_auto_reject_reason", "")):
        assert out.get(k) == v, f"REVISE=新一轮，{k} 必须对称重置（陈旧值污染路由/归因）"
