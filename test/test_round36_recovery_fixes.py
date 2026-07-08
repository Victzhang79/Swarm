"""round36 治本批（执行/恢复层）——取证见 E2E_ROUND35_REGISTER.md round36 段。

P0：worker 编码时引用【全场无生产者的内部类型/包】(round36 实证 st-12-1 引 TwoFactorSetupVO
    →全计划无 owner→连坐炸 62/64)。治=区分"真死上游"(连坐放弃对) vs "自造无生产者引用"
    (授消费者 allow_any + 提示本模块补建被引类型 + 重试，按 targeted_recovery_counts 熔断)。
#9：L2/执行失败触发的 replan 复用了覆盖重试已耗尽的 plan_retry_count → 只剩 1 轮即 3/3 耗尽
    CONFIRM reject。治=replan 是新规划目标，重置 plan_retry_count(replan_count 独立熔断封顶)。
"""
import asyncio
from unittest.mock import patch

from swarm.brain.nodes import handle_failure
from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput


def _st(sid, depends=None):
    return SubTask(id=sid, description="d", scope=FileScope(writable=[f"{sid}.java"]),
                   depends_on=depends or [])


def _wo_blocked(sid, pkgs):
    return WorkerOutput(
        subtask_id=sid, diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "internal_pkg_not_built",
                    "blocked_on_packages": pkgs, "not_run_kind": "blocked",
                    "failure_class": "transient"},
        confidence="low")


def _run(state):
    return asyncio.run(handle_failure(state))


# ── P0：无生产者内部包(worker 自造引用) → scope 自愈，不连坐放弃 ──

def test_p0_no_producer_reference_selfheals_not_abandon():
    """自造引用无生产者→授 allow_any + 提示补建 + 重派，绝不直接连坐放弃依赖闭包。"""
    plan = TaskPlan(subtasks=[_st("st-12"), _st("st-99", depends=["st-12"])])
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {"st-12": _wo_blocked("st-12", ["com.x.domain.vo"])},
        "dispatch_remaining": ["st-99"],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable", return_value=True):
        r = _run(state)
    assert r["failure_strategy"] == "retry_alternate", r.get("failure_strategy")
    assert not r.get("abandoned_subtask_ids"), "无生产者自造引用不该连坐放弃闭包"
    st12 = next(s for s in r["plan"].subtasks if s.id == "st-12")
    assert st12.scope.allow_any is True, "应授 allow_any 让 worker 本模块补建被引类型"
    assert st12.retry_guidance and "新建" in st12.retry_guidance
    assert r["targeted_recovery_counts"]["st-12"] == 1
    assert "st-12" in r["dispatch_remaining"]


def test_p0_selfheal_budget_exhausted_falls_back_to_abandon():
    """自愈预算耗尽(targeted_recovery_counts 达上限)→ 回落连坐放弃(原行为)，不无限自愈。"""
    plan = TaskPlan(subtasks=[_st("st-12"), _st("st-99", depends=["st-12"])])
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {"st-12": _wo_blocked("st-12", ["com.x.domain.vo"])},
        "targeted_recovery_counts": {"st-12": 9},  # 远超 max_retries
        "dispatch_remaining": ["st-99"],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable", return_value=True):
        r = _run(state)
    assert r["failure_strategy"] == "abandon", r.get("failure_strategy")
    assert "st-12" in set(r["abandoned_subtask_ids"])


def test_p0_dead_upstream_still_abandons_not_selfheal():
    """真死上游(依赖已放弃的上游)→ 仍连坐放弃，绝不误走自愈授 allow_any。"""
    plan = TaskPlan(subtasks=[_st("st-5"), _st("st-12", depends=["st-5"])])
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {"st-12": _wo_blocked("st-12", ["com.x.domain.vo"])},
        "abandoned_subtask_ids": ["st-5"],  # 上游已放弃 → dep_hit=True
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable", return_value=True):
        r = _run(state)
    assert r["failure_strategy"] == "abandon", r.get("failure_strategy")
    # 自愈就地 mutate 会改传入 plan 对象；未触发则 allow_any 保持 False。
    st12 = next(s for s in plan.subtasks if s.id == "st-12")
    assert st12.scope.allow_any is False, "依赖已放弃上游=真死，不该自愈"


# ── #9：L2/执行 replan 重置 plan 校验重试预算 ──

def test_fix9_l2_replan_resets_plan_retry_count():
    """L2 触发的 replan 给全新 plan 校验重试预算(清零)，别继承覆盖重试已耗尽的额度。"""
    plan = TaskPlan(subtasks=[_st("st-1")])
    state = {
        "verification_failure": "l2",
        "replan_count": 0,
        "plan_retry_count": 2,  # 覆盖重试已耗到 2
        "failed_subtask_ids": [],
        "subtask_results": {"st-1": _wo_blocked("st-1", [])},
        "plan": plan,
    }
    r = _run(state)
    assert r["failure_strategy"] == "replan", r.get("failure_strategy")
    assert r["plan_retry_count"] == 0, "L2-replan 必须重置 plan 校验重试预算(否则几乎必死)"
