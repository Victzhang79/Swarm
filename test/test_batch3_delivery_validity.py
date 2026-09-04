"""Batch3：交付只接受当前轮真实产物与完整验证链。"""

from __future__ import annotations

import asyncio

import pytest

import swarm.brain.nodes as nodes
from swarm.brain.gates import delivery_outcome, delivery_requires_human_review
from swarm.brain.graph import after_clarify, after_deliver, build_brain_graph
from swarm.brain.nodes import verify
from swarm.brain.state import BrainState
from swarm.types import Complexity, FileScope, HumanDecision, SubTask, TaskPlan, WorkerOutput


_VERIFIED = {
    "plan_valid": True,
    "l2_passed": True,
    "runtime_smoke_passed": True,
    "l3_passed": True,
    "acceptance_passed": True,
    "requirement_denominator_complete": True,
    "failed_subtask_ids": [],
    "failure_escalated": False,
}


def _verified_partial(key: str) -> dict:
    return {
        **_VERIFIED,
        key: ["st-bad"],
        "merged_diff": "diff --git a/good.py b/good.py\n+ok\n",
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {"st-good": {"l1_passed": True}},
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }


@pytest.mark.parametrize("key", [
    "abandoned_subtask_ids",
    "give_up_isolated_ids",
    "merge_rebase_dropped",
    "dispatch_remaining",
    "partial_salvage_ids",
])
@pytest.mark.parametrize("missing", ["merged_diff", "success_product", "validation"])
def test_every_partial_class_requires_current_deliverable_evidence(key, missing):
    state = _verified_partial(key)
    if missing == "merged_diff":
        state["merged_diff"] = ""
    elif missing == "success_product":
        state["subtask_results"] = {}
    else:
        state["runtime_smoke_passed"] = None
        state["runtime_smoke_skipped"] = False

    assert delivery_requires_human_review(state) is False
    assert delivery_outcome(state) == "FAILED"


def test_verified_partial_still_requires_human_and_can_finish_partial():
    state = _verified_partial("dispatch_remaining")
    assert delivery_requires_human_review(state) is True
    assert delivery_outcome(state) == "PARTIAL"


def test_hard_failure_wins_over_old_verified_partial():
    state = {**_verified_partial("abandoned_subtask_ids"), "plan_valid": False}
    assert delivery_requires_human_review(state) is False
    assert delivery_outcome(state) == "FAILED"


def test_clarify_blocked_accept_cannot_bypass_execution_and_validation():
    state = {
        "clarify_blocked_by_facts": True,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }
    assert after_clarify(state) == "deliver"
    assert delivery_outcome(state) == "FAILED"
    assert after_deliver(state) == "learn_failure"


def test_accept_requires_positive_validation_chain_even_without_partial():
    state = {"human_decision": HumanDecision.ACCEPT, "delivery_reviewed": True}
    assert delivery_outcome(state) == "FAILED"
    assert delivery_outcome({**state, **_VERIFIED}) == "DONE"


def test_old_checkpoint_cannot_invent_denominator_or_accept_provenance():
    """旧 checkpoint 缺新账时必须拒绝，不能把“键缺失”解释成已完整/已审核。"""
    old_state = {
        **{key: value for key, value in _VERIFIED.items()
           if key != "requirement_denominator_complete"},
        "human_decision": HumanDecision.ACCEPT,
    }
    assert delivery_outcome(old_state) == "FAILED"


def test_incomplete_denominator_is_reviewable_partial_not_done():
    state = {
        **_VERIFIED,
        "requirement_denominator_complete": False,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }
    assert delivery_outcome(state) == "PARTIAL"


def test_unreviewed_accept_requires_explicit_auto_accept_provenance():
    state = {
        **_VERIFIED,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": False,
    }
    assert delivery_outcome(state) == "FAILED"
    assert delivery_outcome({**state, "auto_accept": True}) == "DONE"


def test_missing_acceptance_result_is_not_an_explicit_skip():
    old_state = {
        **{key: value for key, value in _VERIFIED.items() if key != "acceptance_passed"},
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }
    assert delivery_outcome(old_state) == "FAILED"
    assert delivery_outcome({**old_state, "acceptance_passed": None}) == "DONE"


@pytest.mark.parametrize("polluted", ["false", "true", 0, 1, [], {}])
def test_acceptance_result_only_accepts_explicit_bool_or_none(polluted):
    state = {
        **_VERIFIED,
        "acceptance_passed": polluted,
        "auto_accept": True,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": False,
    }
    assert delivery_outcome(state) == "FAILED"


def test_missing_plan_validation_cannot_reuse_current_delivery_chain():
    old_state = {
        **{key: value for key, value in _VERIFIED.items() if key != "plan_valid"},
        "auto_accept": True,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": False,
    }
    assert delivery_outcome(old_state) == "FAILED"


@pytest.mark.parametrize("failure_patch", [
    {"human_decision": HumanDecision.REJECT},
    {"human_decision": HumanDecision.ACCEPT, "plan_valid": False},
])
def test_learn_success_preflight_blocks_finalizer(monkeypatch, failure_patch):
    finalizer_calls: list[tuple] = []

    async def _finalizer(*args):
        finalizer_calls.append(args)
        raise AssertionError("交付失败不得触发 finalizer")

    async def _failure(_state):
        return {"learn_summary": "failure recorded"}

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _finalizer)
    monkeypatch.setattr(nodes, "learn_failure", _failure)
    out = asyncio.run(nodes.learn_success({
        **_VERIFIED,
        "task_id": "t-preflight",
        "project_id": "p",
        "task_description": "bad delivery",
        "complexity": Complexity.SIMPLE,
        "merged_diff": "diff --git a/a b/a\n+x\n",
        **failure_patch,
    }))
    assert finalizer_calls == []
    assert out == {"learned": False, "learn_summary": "failure recorded"}


@pytest.mark.parametrize("missing_keys", [
    ("requirement_denominator_complete", "delivery_reviewed"),
    ("acceptance_passed",),
    ("plan_valid",),
    ("delivery_reviewed",),
])
def test_learn_success_old_checkpoint_stops_before_finalizer(monkeypatch, missing_keys):
    finalizer_calls: list[tuple] = []

    async def _finalizer(*args):
        finalizer_calls.append(args)
        raise AssertionError("旧 checkpoint 不得触发 finalizer")

    async def _failure(_state):
        return {"learn_summary": "old checkpoint rejected"}

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _finalizer)
    monkeypatch.setattr(nodes, "learn_failure", _failure)
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "/project")
    complete_state = {
        **_VERIFIED,
        "task_id": "t-old-checkpoint",
        "project_id": "p",
        "task_description": "old state",
        "complexity": Complexity.SIMPLE,
        "merged_diff": "diff --git a/a b/a\n+x\n",
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }
    old_state = {
        key: value for key, value in complete_state.items() if key not in missing_keys
    }
    out = asyncio.run(nodes.learn_success(old_state))
    assert finalizer_calls == []
    assert out == {"learned": False, "learn_summary": "old checkpoint rejected"}


def test_merge_filters_plan_external_result_without_partial_salvage(monkeypatch):
    """普通/重试 checkpoint 也必须在 MERGE 咽喉按当前 plan 剔除旧轮结果。"""
    plan = TaskPlan(subtasks=[
        SubTask(id="st-current", description="当前", scope=FileScope(writable=["current.py"])),
    ])
    current_diff = (
        "diff --git a/current.py b/current.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/current.py\n@@ -0,0 +1 @@\n+CURRENT = True\n"
    )
    stale_diff = (
        "diff --git a/stale.py b/stale.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/stale.py\n@@ -0,0 +1 @@\n+STALE = True\n"
    )
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    out = nodes.merge({
        "task_id": "t-normal-resume",
        "project_id": "",
        "plan": plan,
        "subtask_results": {
            "st-current": WorkerOutput(
                subtask_id="st-current", diff=current_diff, summary="current", l1_passed=True,
            ),
            "st-old": WorkerOutput(
                subtask_id="st-old", diff=stale_diff, summary="stale", l1_passed=True,
            ),
        },
    })
    assert "CURRENT = True" in out["merged_diff"]
    assert "STALE = True" not in out["merged_diff"]
    assert set(out["subtask_results"]) == {"st-current"}
    assert "merge_dropped_out_of_plan:st-old" in out["degraded_reasons"]


def test_merge_rejects_results_when_current_plan_is_missing(monkeypatch):
    stale_diff = (
        "diff --git a/stale.py b/stale.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/stale.py\n@@ -0,0 +1 @@\n+STALE = True\n"
    )
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    out = nodes.merge({
        "task_id": "t-no-current-plan",
        "project_id": "",
        "plan": None,
        "subtask_results": {
            "st-old": WorkerOutput(
                subtask_id="st-old", diff=stale_diff, summary="stale", l1_passed=True,
            ),
        },
    })
    assert "STALE = True" not in out["merged_diff"]
    assert out["failure_escalated"] is True
    assert out["verification_failure"] == "merge_plan_missing"
    assert out["subtask_results"] == {}
    assert "merge_dropped_out_of_plan:st-old" in out["degraded_reasons"]


def test_revision_graph_reextracts_requirements_before_replanning_and_dispatch():
    graph = build_brain_graph()
    assert ("revision", "extract_requirements") in graph.edges
    assert ("extract_requirements", "plan") in graph.edges
    assert ("revision", "validate_plan") not in graph.edges
    assert ("revision", "dispatch") not in graph.edges


def test_revision_invalidates_old_requirement_denominator(monkeypatch):
    monkeypatch.setattr(
        nodes,
        "_get_brain_llm",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    monkeypatch.setattr(
        "swarm.brain.contract_utils.resolve_plan_conflicts", lambda *_a, **_k: {}
    )

    out = asyncio.run(nodes.revision({
        "task_id": "t-revise-requirements",
        "project_id": "p",
        "task_description": "原始需求：支持登录。",
        "clarify_summary": "登录需要审计。",
        "revision_feedback": "还必须新增双因素认证。",
        "plan": None,
        "subtask_results": {},
        "requirement_items": [{"id": "old", "text": "支持登录"}],
        "requirement_denominator_complete": True,
        "requirement_denominator_reason": "",
        "baseline_covered": ["old"],
        "baseline_ineligible_reqs": ["old"],
    }))

    assert out["requirement_items"] == []
    assert out["requirement_denominator_complete"] is False
    assert out["requirement_denominator_reason"] == "revision_pending_reextract"
    assert out["baseline_covered"] == []
    assert out["baseline_ineligible_reqs"] == []
    assert "还必须新增双因素认证" in out["clarify_summary"]


@pytest.mark.parametrize("failure", ["config", "push"])
def test_configured_gitlab_exception_never_falls_back_to_staging(monkeypatch, failure):
    import swarm.brain.l3_gitlab as gitlab

    staging_calls: list[str] = []

    class _Staging:
        async def ainvoke(self, _messages):
            staging_calls.append("called")
            return type("R", (), {"content": '{"l3_passed": true}'})()

    monkeypatch.setenv("SWARM_STAGING_URL", "https://staging.invalid")
    monkeypatch.setattr(verify, "effective_complexity", lambda _state: Complexity.COMPLEX)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _Staging())
    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: "/project")
    if failure == "config":
        monkeypatch.setattr(
            gitlab, "gitlab_configured", lambda: (_ for _ in ()).throw(OSError("config")),
        )
    else:
        monkeypatch.setattr(gitlab, "gitlab_configured", lambda: True)
        monkeypatch.setattr(gitlab, "l3_push_enabled", lambda: True)
        monkeypatch.setattr(
            gitlab, "push_merged_diff_branch",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("push")),
        )

    out = asyncio.run(verify.verify_l3({
        "task_id": "t-gitlab-error",
        "project_id": "p",
        "merged_diff": "diff --git a/a b/a\n+x\n",
    }))
    assert staging_calls == []
    assert out["l3_passed"] is None and out["l3_skipped"] is True
    assert out["l3_skip_reason"] in {"gitlab_config_error", "gitlab_error"}
    assert out["l3_branch"] == ""


def test_merge_rebase_dropped_is_really_clearable_by_langgraph_reducer():
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(BrainState)
    graph.add_node("clear", lambda _state: {"merge_rebase_dropped": []})
    graph.add_edge(START, "clear")
    graph.add_edge("clear", END)
    out = graph.compile().invoke({"merge_rebase_dropped": ["old"]})
    assert out["merge_rebase_dropped"] == []


def _accepted_partial_runner_state() -> dict:
    return {
        **_VERIFIED,
        "task_description": "x",
        "human_decision": "accept",
        "delivery_reviewed": True,
        "dispatch_remaining": ["st-never-ran"],
        "subtask_results": {"st-1": {"l1_passed": True, "output": "ok"}},
        "plan": {"subtasks": [{"id": "st-1"}, {"id": "st-never-ran"}]},
        "merged_diff": "diff --git a/f b/f\n+x\n",
        "acceptance_passed": None,
    }


def test_post_run_accepted_partial_cas_rejected_announces_error_not_partial(monkeypatch):
    from swarm.infra.degrade import degrade_counts
    from test.test_a8l1_l2_cancel_account_and_cas_notification import (
        _drain, _post_run_store,
    )

    runner, _store = _post_run_store(monkeypatch, reject_status_write=True)
    monkeypatch.setattr(runner, "_sweep_unverified_footprints", lambda *a, **k: None)
    audits: list[str] = []
    monkeypatch.setattr(runner, "audit", lambda event, **kw: audits.append(event))
    topic = runner._FanoutTopic()
    sub = topic.subscribe()
    asyncio.run(runner._handle_post_run("t-h1", _accepted_partial_runner_state(), topic))
    events = _drain(sub)

    assert not [e for e in events if e.get("step") in ("complete", "done")]
    errors = [e for e in events if e.get("step") == "error"]
    assert len(errors) == 1 and "CANCELLED" in (errors[0].get("message") or "")
    assert "task_partial" not in audits
    assert degrade_counts().get("brain.runner.terminal_announce_suppressed", 0) == 1


def test_post_run_accepted_partial_cas_accepted_still_announces_partial(monkeypatch):
    from test.test_a8l1_l2_cancel_account_and_cas_notification import (
        _drain, _post_run_store,
    )

    runner, _store = _post_run_store(monkeypatch, reject_status_write=False)
    monkeypatch.setattr(runner, "_sweep_unverified_footprints", lambda *a, **k: None)
    audits: list[str] = []
    monkeypatch.setattr(runner, "audit", lambda event, **kw: audits.append(event))
    topic = runner._FanoutTopic()
    sub = topic.subscribe()
    asyncio.run(runner._handle_post_run("t-h1", _accepted_partial_runner_state(), topic))
    events = _drain(sub)

    completes = [e for e in events
                 if e.get("step") == "complete" and e.get("status") == "partial"]
    assert len(completes) == 1
    assert not [e for e in events if e.get("step") == "error"]
    assert "task_partial" in audits
