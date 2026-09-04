"""Batch3：部分交付必须先合并并通过当前轮确定性验证链。"""

from __future__ import annotations

import asyncio

import pytest

import swarm.brain.nodes as nodes
from swarm.brain.gates import (
    can_auto_accept_delivery,
    delivery_outcome,
    delivery_requires_human_review,
    partial_delivery_ids,
)
from swarm.brain.graph import after_deliver, after_handle_failure, after_merge, build_brain_graph
from swarm.types import (
    Complexity,
    FileScope,
    HumanDecision,
    SubTask,
    TaskPlan,
    WorkerOutput,
)


_VERIFIED = {
    "plan_valid": True,
    "l2_passed": True,
    "l3_passed": True,
    "runtime_smoke_passed": True,
    "acceptance_passed": True,
    "failed_subtask_ids": [],
    "failure_escalated": False,
    "requirement_denominator_complete": True,
    "requirement_denominator_reason": "",
}


def test_mr_creation_requires_exact_current_l3_branch(monkeypatch):
    """当前轮无已发布精确 ref 时，不得拿旧/猜测分支创建 MR。"""
    import swarm.brain.l3_gitlab as gitlab

    mr_calls: list[dict] = []
    monkeypatch.setenv("SWARM_GITLAB_MR_ON_ACCEPT", "1")
    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: "/project")
    monkeypatch.setattr(gitlab, "gitlab_configured", lambda: True)
    monkeypatch.setattr(
        gitlab,
        "create_merge_request",
        lambda **kwargs: (mr_calls.append(kwargs) or ("https://mr.invalid/1", None)),
    )

    async def _persist(_state, _parsed):
        return {"persisted": False, "reason": "test"}

    async def _deliver(*_args):
        return {
            "ap": {"ok": True, "applied": ["a"], "failed": []},
            "out_files": ["a"],
            "wm": {},
            "commit": {"ok": True, "committed": False},
        }

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _deliver)
    monkeypatch.setattr("swarm.brain.learn_store.persist_learn_success", _persist)
    monkeypatch.setattr(
        "swarm.knowledge.hooks.schedule_incremental_update", lambda *_a, **_k: None,
    )
    asyncio.run(nodes.learn_success({
        **_VERIFIED,
        "task_id": "t-no-branch",
        "project_id": "p",
        "task_description": "accepted",
        "complexity": Complexity.SIMPLE,
        "merged_diff": "diff --git a/a b/a",
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
        "l3_branch": "",
    }))

    assert mr_calls == []


def test_verified_partial_salvage_requires_human_and_finishes_partial(monkeypatch):
    """经完整验证链的 partial salvage 才能转人工；接受后是 PARTIAL。"""
    reviews: list[dict] = []
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda payload: reviews.append(payload) or {"decision": "accept"},
    )
    state = {
        **_VERIFIED,
        "auto_accept": True,
        "task_id": "t-escalated",
        "merged_diff": "diff --git a/good.py b/good.py\n+ok\n",
        "failure_escalated": False,
        "failed_subtask_ids": [],
        "partial_salvage_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {
            "st-good": {"l1_passed": True, "diff": "diff --git a/good.py b/good.py"},
            "st-bad": {"l1_passed": False},
        },
    }

    assert delivery_requires_human_review(state) is True
    assert partial_delivery_ids(state) == ["st-bad"]
    delivered = nodes.deliver(state)
    terminal = {**state, **delivered}

    assert reviews and delivered["delivery_reviewed"] is True
    assert delivery_outcome(terminal) == "PARTIAL"
    assert after_deliver(terminal) == "learn_success"


def test_exhausted_simple_failure_routes_successful_sibling_through_merge_and_verification(
    monkeypatch,
):
    """真实失败链必须先合并成功兄弟，再走 L2/runtime/L3，不能从 merge 前直达交付。"""
    plan = TaskPlan(subtasks=[
        SubTask(id="st-good", description="实现", scope=FileScope(writable=["good.py"])),
        SubTask(id="st-bad", description="测试", scope=FileScope(writable=["bad.py"])),
    ])
    good_diff = (
        "diff --git a/good.py b/good.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/good.py\n"
        "@@ -0,0 +1 @@\n"
        "+ok = True\n"
    )
    state = {
        "task_id": "t-partial-chain",
        "project_id": "",
        "complexity": Complexity.SIMPLE,
        "plan": plan,
        "failed_subtask_ids": ["st-bad"],
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff=good_diff, summary="ok", l1_passed=True,
            ),
            "st-bad": WorkerOutput(
                subtask_id="st-bad", diff="", summary="failed", l1_passed=False,
            ),
        },
        "subtask_retry_counts": {"st-bad": 3},
        "dispatch_remaining": [],
    }

    handled = asyncio.run(nodes.handle_failure(state))
    routed = {**state, **handled}

    assert handled["failure_strategy"] == "partial_merge"
    assert handled["failure_escalated"] is False
    assert handled["failed_subtask_ids"] == []
    assert handled["partial_salvage_ids"] == ["st-bad"]
    assert after_handle_failure(routed) == "merge"

    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    merged = nodes.merge(routed)
    merged_state = {**routed, **merged}
    assert "+ok = True" in merged_state["merged_diff"]
    assert after_merge(merged_state) == "verify_l2"
    assert partial_delivery_ids(merged_state) == []

    verified = {
        **merged_state,
        "plan_valid": True,
        "l2_passed": True,
        "runtime_smoke_passed": True,
        "l3_passed": True,
        "acceptance_passed": None,
    }
    assert partial_delivery_ids(verified) == ["st-bad"]


def test_exhausted_complex_replan_with_current_success_routes_partial_merge(monkeypatch):
    """COMPLEX 的 replan 守卫耗尽后也必须保成果进验证链，不能绕过 merge。"""
    plan = TaskPlan(subtasks=[
        SubTask(id="st-good", description="实现", scope=FileScope(writable=["good.py"])),
        SubTask(id="st-bad", description="测试", scope=FileScope(writable=["bad.py"])),
    ])
    state = {
        "task_id": "t-complex-partial",
        "project_id": "",
        "complexity": Complexity.COMPLEX,
        "plan": plan,
        "failed_subtask_ids": ["st-bad"],
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff="--- /dev/null\n+++ b/good.py\n@@ -0,0 +1 @@\n+ok=1\n",
                summary="ok", l1_passed=True,
            ),
            "st-bad": WorkerOutput(
                subtask_id="st-bad", diff="", summary="failed", l1_passed=False,
            ),
        },
        "subtask_retry_counts": {"st-bad": 3},
        "dispatch_remaining": [],
        "replan_count": 0,
    }

    class _LLM:
        async def ainvoke(self, _messages):
            class _Response:
                content = '{"strategy":"replan","reasoning":"capability exhausted"}'
            return _Response()

    async def _none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _LLM())
    monkeypatch.setattr("swarm.brain.nodes.failure._targeted_redecompose", _none)
    monkeypatch.setattr("swarm.brain.nodes.failure._give_up_preserve_build", _none)

    handled = asyncio.run(nodes.handle_failure(state))

    assert handled["failure_strategy"] == "partial_merge"
    assert handled["partial_salvage_ids"] == ["st-bad"]
    assert set(handled["subtask_results"]) == {"st-good"}
    assert after_handle_failure({**state, **handled}) == "merge"


def test_handle_failure_graph_wires_partial_merge_to_merge_node():
    """路由函数新增标签后，生产图也必须真实接到 MERGE。"""
    graph = build_brain_graph()
    ends = set()
    for spec in graph.branches["handle_failure"].values():
        ends.update((spec.ends or {}).values())
    assert "merge" in ends


def test_new_retry_disposition_clears_stale_partial_salvage(monkeypatch):
    """验证失败后的新恢复轮必须清旧 partial salvage，避免历史粘滞。"""
    state = {
        "complexity": Complexity.SIMPLE,
        "plan": TaskPlan(subtasks=[
            SubTask(id="st-bad", description="修复", scope=FileScope(writable=["bad.py"])),
        ]),
        "failed_subtask_ids": ["st-bad"],
        "partial_salvage_ids": ["st-bad"],
        "subtask_results": {
            "st-bad": WorkerOutput(
                subtask_id="st-bad", diff="", summary="failed", l1_passed=False,
            ),
        },
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
    }
    out = asyncio.run(nodes.handle_failure(state))
    assert out["failure_strategy"] == "retry"
    assert out["partial_salvage_ids"] == []


def test_l2_targeted_retry_preserves_unrelated_partial_salvage_through_revalidation(monkeypatch):
    """验证链重做别的归因项时，原 partial 缺项必须跨轮保留，最终仍是 PARTIAL。"""
    plan = TaskPlan(subtasks=[
        SubTask(id="st-good", description="实现", scope=FileScope(writable=["good.py"])),
        SubTask(id="st-bad", description="未完成", scope=FileScope(writable=["bad.py"])),
        SubTask(id="st-fix", description="集成修复", scope=FileScope(writable=["fix.py"])),
    ])
    state = {
        "complexity": Complexity.COMPLEX,
        "plan": plan,
        "partial_salvage_ids": ["st-bad"],
        "verification_failure": "l2",
        "l2_targeted": True,
        "failed_subtask_ids": ["st-fix"],
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff="+good\n", summary="good", l1_passed=True,
            ),
            "st-fix": WorkerOutput(
                subtask_id="st-fix", diff="+broken\n", summary="bad integration", l1_passed=True,
            ),
        },
        "dispatch_remaining": [],
        "replan_count": 0,
    }
    retried = asyncio.run(nodes.handle_failure(state))
    assert retried["failure_strategy"] == "retry"
    assert retried["partial_salvage_ids"] == ["st-bad"]

    good_diff = (
        "diff --git a/good.py b/good.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/good.py\n@@ -0,0 +1 @@\n+good = True\n"
    )
    fix_diff = (
        "diff --git a/fix.py b/fix.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/fix.py\n@@ -0,0 +1 @@\n+fixed = True\n"
    )
    recovered = {
        **state,
        **retried,
        "dispatch_remaining": [],
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff=good_diff, summary="good", l1_passed=True,
            ),
            "st-fix": WorkerOutput(
                subtask_id="st-fix", diff=fix_diff, summary="fixed", l1_passed=True,
            ),
        },
    }
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    merged = nodes.merge(recovered)
    verified = {
        **recovered,
        **merged,
        "plan_valid": True,
        "l2_passed": True,
        "runtime_smoke_passed": True,
        "l3_passed": True,
        "acceptance_passed": None,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }
    assert partial_delivery_ids(verified) == ["st-bad"]
    assert delivery_outcome(verified) == "PARTIAL"


@pytest.mark.parametrize("failure_patch", [
    {"failure_escalated": True, "failed_subtask_ids": ["st-bad"], "l2_passed": False},
    {"verification_failure": "l3", "l3_passed": False},
    {"runtime_smoke_passed": False},
    {"acceptance_passed": False},
])
def test_human_accept_cannot_override_deterministic_delivery_failure(failure_patch):
    state = {
        **_VERIFIED,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
        **failure_patch,
    }
    assert delivery_outcome(state) == "FAILED"
    assert after_deliver(state) == "learn_failure"


def test_explicit_complex_escalation_with_current_success_still_validates_partial(monkeypatch):
    plan = TaskPlan(subtasks=[
        SubTask(id="st-good", description="实现", scope=FileScope(writable=["good.py"])),
        SubTask(id="st-bad", description="失败", scope=FileScope(writable=["bad.py"])),
    ])

    class _LLM:
        async def ainvoke(self, _messages):
            class _Response:
                content = '{"strategy":"escalate","reasoning":"human needed"}'
            return _Response()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _LLM())
    out = asyncio.run(nodes.handle_failure({
        "complexity": Complexity.COMPLEX,
        "plan": plan,
        "failed_subtask_ids": ["st-bad"],
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff="+good\n", summary="good", l1_passed=True,
            ),
            "st-bad": WorkerOutput(
                subtask_id="st-bad", diff="", summary="bad", l1_passed=False,
            ),
        },
    }))
    assert out["failure_strategy"] == "partial_merge"
    assert out["partial_salvage_ids"] == ["st-bad"]
    assert after_handle_failure(out) == "merge"


def test_partial_merge_filters_out_of_plan_checkpoint_results():
    plan = TaskPlan(subtasks=[
        SubTask(id="st-good", description="实现", scope=FileScope(writable=["good.py"])),
        SubTask(id="st-bad", description="失败", scope=FileScope(writable=["bad.py"])),
    ])
    out = asyncio.run(nodes.handle_failure({
        "complexity": Complexity.SIMPLE,
        "plan": plan,
        "failed_subtask_ids": ["st-bad"],
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff="+good\n", summary="good", l1_passed=True,
            ),
            "st-bad": WorkerOutput(
                subtask_id="st-bad", diff="", summary="bad", l1_passed=False,
            ),
            "st-old": WorkerOutput(
                subtask_id="st-old", diff="+stale\n", summary="stale", l1_passed=True,
            ),
        },
        "subtask_retry_counts": {"st-bad": 3},
    }))
    assert set(out["subtask_results"]) == {"st-good"}


def test_merge_chokepoint_filters_out_of_plan_result_after_checkpoint_resume(monkeypatch):
    """从 handle_failure checkpoint 直恢 MERGE 也不能合入计划外旧结果。"""
    plan = TaskPlan(subtasks=[
        SubTask(id="st-good", description="实现", scope=FileScope(writable=["good.py"])),
        SubTask(id="st-bad", description="失败", scope=FileScope(writable=["bad.py"])),
    ])
    current_diff = (
        "diff --git a/good.py b/good.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/good.py\n@@ -0,0 +1 @@\n+current = True\n"
    )
    stale_diff = (
        "diff --git a/stale.py b/stale.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/stale.py\n@@ -0,0 +1 @@\n+stale = True\n"
    )
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    out = nodes.merge({
        "task_id": "t-resume",
        "project_id": "",
        "plan": plan,
        "partial_salvage_ids": ["st-bad"],
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff=current_diff, summary="current", l1_passed=True,
            ),
            "st-old": WorkerOutput(
                subtask_id="st-old", diff=stale_diff, summary="stale", l1_passed=True,
            ),
        },
    })
    assert "current = True" in out["merged_diff"]
    assert "stale = True" not in out["merged_diff"]
    assert set(out["subtask_results"]) == {"st-good"}
    assert "merge_dropped_out_of_plan:st-old" in out["degraded_reasons"]


def test_merge_partial_salvage_without_current_plan_fails_closed():
    out = nodes.merge({
        "task_id": "t-no-plan",
        "project_id": "",
        "plan": None,
        "partial_salvage_ids": ["st-bad"],
        "subtask_results": {},
    })
    assert out["failure_escalated"] is True
    assert out["verification_failure"] == "partial_salvage_plan_missing"


def test_merge_accounts_unlisted_current_l1_failure_in_partial_salvage(monkeypatch):
    plan = TaskPlan(subtasks=[
        SubTask(id="st-good", description="实现", scope=FileScope(writable=["good.py"])),
        SubTask(id="st-bad", description="失败", scope=FileScope(writable=["bad.py"])),
        SubTask(id="st-hidden", description="旧坏账", scope=FileScope(writable=["hidden.py"])),
    ])
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    out = nodes.merge({
        "task_id": "t-hidden-fail",
        "project_id": "",
        "plan": plan,
        "partial_salvage_ids": ["st-bad"],
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff="+good\n", summary="good", l1_passed=True,
            ),
            "st-hidden": WorkerOutput(
                subtask_id="st-hidden", diff="+bad\n", summary="hidden", l1_passed=False,
            ),
        },
    })
    assert out["partial_salvage_ids"] == ["st-bad", "st-hidden"]


def test_delivery_review_payload_lists_partial_ids_directly():
    payload = nodes._deliver_review_payload({
        **_VERIFIED,
        "merged_diff": "diff --git a/good.py b/good.py\n+good\n",
        "partial_salvage_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {"st-good": {"l1_passed": True}},
    })
    assert payload["partial_delivery_ids"] == ["st-bad"]
    assert payload["partial_delivery_total"] == 1


def test_unverified_partial_salvage_cannot_be_auto_or_human_accepted(monkeypatch):
    """异常绕过验证到 DELIVER 时必须失败学习，不能把旧人工 ACCEPT 包装成 DONE。"""
    reviews: list[dict] = []
    monkeypatch.setattr(
        nodes, "interrupt", lambda payload: reviews.append(payload) or {"decision": "accept"},
    )
    state = {
        **_VERIFIED,
        "auto_accept": True,
        "merged_diff": "diff --git a/good.py b/good.py\n+good\n",
        "l3_passed": None,
        "l3_skipped": False,
        "partial_salvage_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {"st-good": {"l1_passed": True}},
    }
    allow, reason = can_auto_accept_delivery(state)
    delivered = nodes.deliver(state)
    terminal = {**state, **delivered}

    assert allow is False
    assert "partial_delivery_unverified" in reason
    assert reviews == []
    assert delivered["human_decision"] == HumanDecision.REJECT
    assert delivery_outcome(terminal) == "FAILED"
    assert after_deliver(terminal) == "learn_failure"


def test_partial_merge_without_current_diff_fails_before_verification(monkeypatch):
    """成功兄弟只有 L1 标记、没有可合并产物时，不得靠人工接受学成 PARTIAL。"""
    state = {
        "task_id": "t-empty-partial",
        "project_id": "",
        "partial_salvage_ids": ["st-bad"],
        "plan": TaskPlan(subtasks=[
            SubTask(id="st-good", description="实现", scope=FileScope(writable=["good.py"])),
            SubTask(id="st-bad", description="测试", scope=FileScope(writable=["bad.py"])),
        ]),
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", diff="", summary="claimed success", l1_passed=True,
            ),
        },
        "failed_subtask_ids": [],
        "failure_escalated": False,
    }
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    merged = nodes.merge(state)
    terminal = {
        **state,
        **merged,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }

    assert merged["failure_escalated"] is True
    assert merged["verification_failure"] == "partial_salvage_empty_merge"
    assert after_merge({**state, **merged}) == "deliver"
    assert delivery_outcome(terminal) == "FAILED"


def test_revision_clears_current_round_delivery_and_l3_facts(monkeypatch):
    """修订轮不得复用旧 merged diff、分支或部分交付账。"""
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(nodes, "_get_project_path", lambda _project_id: "")
    monkeypatch.setattr(
        "swarm.brain.contract_utils.resolve_plan_conflicts", lambda *_a, **_k: {},
    )
    out = asyncio.run(nodes.revision({
        "task_id": "t-revision-clean",
        "project_id": "",
        "task_description": "change it",
        "revision_feedback": "try again",
        "merged_diff": "diff --git a/old.py b/old.py\n+old\n",
        "l3_branch": "swarm/old-branch",
        "merge_conflicts": [{"file": "old.py"}],
        "rebase_subtask_ids": ["old"],
        "merge_owner_drops": [{"file": "old.py"}],
        "merge_owner_unions": [{"file": "old.py"}],
        "abandoned_subtask_ids": ["old-abandoned"],
        "give_up_isolated_ids": ["old-giveup"],
        "merge_rebase_dropped": ["old-rebase"],
        "partial_salvage_ids": ["old-failure"],
        "subtask_results": {},
    }))

    for key, empty in (
        ("merged_diff", ""),
        ("l3_branch", ""),
        ("merge_conflicts", []),
        ("rebase_subtask_ids", []),
        ("merge_owner_drops", []),
        ("merge_owner_unions", []),
        ("abandoned_subtask_ids", []), ("give_up_isolated_ids", []),
        ("merge_rebase_dropped", []),
        ("partial_salvage_ids", []),
    ):
        assert out.get(key) == empty, f"revision 未清本轮事实 {key}"

    revision_id = out["dispatch_remaining"][0]
    assert out["plan_valid"] is False
    assert out["plan_retry_count"] == 0

    class _EscalateLLM:
        async def ainvoke(self, _messages):
            class _Response:
                content = '{"strategy":"escalate","reasoning":"revision failed"}'
            return _Response()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _EscalateLLM())
    failed_round = {
        **out,
        "complexity": Complexity.COMPLEX,
        "failed_subtask_ids": [revision_id],
        "subtask_results": {
            revision_id: WorkerOutput(
                subtask_id=revision_id, diff="", summary="revision failed", l1_passed=False,
            ),
        },
    }
    handled = asyncio.run(nodes.handle_failure(failed_round))
    final_state = {**failed_round, **handled}
    assert after_handle_failure(final_state) == "deliver"
    assert final_state["merged_diff"] == ""
    assert final_state["l3_branch"] == ""


def test_escalated_failure_without_successful_output_is_not_salvageable(monkeypatch):
    """只有失败、没有任何 L1 成功产物时仍应直接失败，不能包装成 PARTIAL。"""
    reviews: list[dict] = []
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda payload: reviews.append(payload) or {"decision": "accept"},
    )
    state = {
        **_VERIFIED,
        "auto_accept": True,
        "task_id": "t-all-failed",
        "merged_diff": "",
        "failure_escalated": True,
        "failed_subtask_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-bad"}]},
        "subtask_results": {"st-bad": {"l1_passed": False}},
    }

    assert delivery_requires_human_review(state) is False
    assert partial_delivery_ids(state) == []
    delivered = nodes.deliver(state)

    assert reviews == []
    assert delivered["human_decision"] == HumanDecision.REJECT
    assert delivery_outcome({**state, **delivered}) == "FAILED"


@pytest.mark.parametrize("state_patch", [
    {
        "partial_salvage_ids": ["st-bad"],
        "l2_passed": False,
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
    },
    {"partial_salvage_ids": ["st-bad"], "plan": None},
    {"partial_salvage_ids": ["st-bad"], "plan": {"subtasks": []}},
    {
        "partial_salvage_ids": ["st-old-round"],
        "plan": {"subtasks": [{"id": "st-good"}]},
    },
    {
        "partial_salvage_ids": ["st-bad", "st-old-round"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
    },
])
def test_arbitrary_or_stale_failed_ids_are_not_partial(state_patch):
    """仅当前轮正式 escalation 可抢救；瞬态/旧轮 failed 账不能凭一个成功结果变 PARTIAL。"""
    state = {
        **_VERIFIED,
        "failed_subtask_ids": ["st-bad"],
        "subtask_results": {"st-good": {"l1_passed": True}},
        **state_patch,
    }

    assert partial_delivery_ids(state) == []


def test_human_reject_of_verified_partial_salvage_remains_failed(monkeypatch):
    monkeypatch.setattr(nodes, "interrupt", lambda _payload: {"decision": "reject"})
    state = {
        **_VERIFIED,
        "auto_accept": True,
        "merged_diff": "diff --git a/good.py b/good.py\n+good\n",
        "failure_escalated": False,
        "failed_subtask_ids": [],
        "partial_salvage_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {
            "st-good": {"l1_passed": True},
            "st-bad": {"l1_passed": False},
        },
    }
    delivered = nodes.deliver(state)

    assert delivered["delivery_reviewed"] is True
    assert delivery_outcome({**state, **delivered}) == "FAILED"
    assert after_deliver({**state, **delivered}) == "learn_failure"


def test_runner_persists_human_accepted_verified_salvage_as_partial(monkeypatch):
    """runner 必须消费同一 salvage 口径，落 PARTIAL 并在终态事件披露失败项。"""
    import swarm.brain.runner as runner

    writes: list[dict] = []
    events: list[dict] = []

    async def _emit(_queue, event):
        events.append(event)

    monkeypatch.setattr(runner, "_emit", _emit)
    monkeypatch.setattr(runner, "_sync_task_from_state", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_emit_task_notification", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_attach_observability_account", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner.store, "get_task",
        lambda tid: {"id": tid, "project_id": "p", "description": "d"},
    )
    monkeypatch.setattr(
        runner.store, "update_task",
        lambda tid, **kw: writes.append(kw) or {"id": tid, **kw},
    )
    monkeypatch.setattr(runner.store, "estimate_token_usage", lambda **_kw: {})
    monkeypatch.setattr(runner.store, "compute_task_duration_seconds", lambda _rec: 1.0)

    state = {
        **_VERIFIED,
        "auto_accept": True,
        "merged_diff": "diff --git a/good.py b/good.py\n+good\n",
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
        "failure_escalated": False,
        "failed_subtask_ids": [],
        "partial_salvage_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {
            "st-good": {"l1_passed": True},
            "st-bad": {"l1_passed": False},
        },
        "task_description": "d",
    }
    asyncio.run(runner._handle_post_run("t-salvage", state, None))

    assert [row["status"] for row in writes if row.get("status")] == ["PARTIAL"]
    complete = next(event for event in events if event.get("step") == "complete")
    assert complete["status"] == "partial"
    assert "st-bad" in complete["message"]


def test_learn_store_records_verified_salvage_as_partial_without_l6(monkeypatch):
    """学习落库与 gates/runner 同源：L2=partial，且绝不写 L6 成功模式。"""
    from swarm.brain import learn_store

    captured: dict = {}

    class _FakeStore:
        async def connect(self):
            return None

        def transaction(self):
            class _Tx:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

            return _Tx()

        async def write_success(self, *_args, **_kwargs):
            captured["wrote_l6"] = True

        async def write_task_summary(self, _project_id, summary):
            captured["outcome"] = summary.outcome

        async def close(self):
            return None

    monkeypatch.setattr(learn_store, "MemoryStore", _FakeStore)
    result = asyncio.run(learn_store.persist_learn_success({
        **_VERIFIED,
        "merged_diff": "diff --git a/good.py b/good.py\n+good\n",
        "project_id": "p",
        "task_id": "t-salvage",
        "complexity": Complexity.COMPLEX,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
        "failure_escalated": False,
        "failed_subtask_ids": [],
        "partial_salvage_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {
            "st-good": {"l1_passed": True},
            "st-bad": {"l1_passed": False},
        },
    }, {"pattern_name": "must-not-be-success"}))

    assert result["persisted"] is True
    assert captured["outcome"] == "partial"
    assert "wrote_l6" not in captured
