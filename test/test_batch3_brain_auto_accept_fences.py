"""Batch3：Brain 自动验收只接受完整需求、完整交付和被测的 L3 变更。"""

from __future__ import annotations

import asyncio
import json

import pytest

import swarm.brain.nodes as nodes
from swarm.brain.gates import (
    can_auto_accept_delivery,
    delivery_outcome,
    delivery_requires_human_review,
)
from swarm.brain.graph import after_deliver
from swarm.brain.nodes import verify
from swarm.brain.requirements_extract import MAX_EXTRACT_RETRIES, extract_requirements
from swarm.memory.pattern_extractor import blocking_degraded_reasons, should_write_success
from swarm.types import Complexity, HumanDecision


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


@pytest.mark.parametrize(
    "denominator_reason",
    [
        "source_truncated",
        "below_expected_after_retries",
        "grounded_items_truncated",
        "empty_source",
        "empty",
    ],
)
def test_auto_accept_rejects_incomplete_requirement_denominator(denominator_reason):
    allow, reason = can_auto_accept_delivery({
        **_VERIFIED,
        "requirement_denominator_complete": False,
        "requirement_denominator_reason": denominator_reason,
    })

    assert allow is False
    assert "requirement" in reason


@pytest.mark.parametrize(
    "partial_key",
    [
        "abandoned_subtask_ids",
        "give_up_isolated_ids",
        "merge_rebase_dropped",
        "dispatch_remaining",
    ],
)
def test_auto_accept_rejects_every_partial_delivery_class(partial_key):
    allow, reason = can_auto_accept_delivery({
        **_VERIFIED,
        partial_key: ["st-1"],
    })

    assert allow is False
    assert "partial_delivery" in reason
    assert "st-1" in reason


def test_partial_auto_accept_requires_human_review(monkeypatch):
    reviews: list[dict] = []
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda payload: reviews.append(payload) or {"decision": "accept"},
    )
    out = nodes.deliver({
        **_VERIFIED,
        "auto_accept": True,
        "task_id": "t-batch3",
        "merged_diff": "diff --git a/a b/a",
        "dispatch_remaining": ["st-never-ran"],
        "plan": {"subtasks": [{"id": "st-done"}, {"id": "st-never-ran"}]},
        "subtask_results": {"st-done": {"l1_passed": True}},
    })

    assert reviews and reviews[0]["type"] == "deliver"
    assert out["human_decision"] == HumanDecision.ACCEPT
    assert out["delivery_reviewed"] is True


def test_incomplete_denominator_auto_accept_requires_human_review(monkeypatch):
    reviews: list[dict] = []
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda payload: reviews.append(payload) or {"decision": "reject"},
    )
    out = nodes.deliver({
        **_VERIFIED,
        "auto_accept": True,
        "task_id": "t-batch3",
        "merged_diff": "diff --git a/a b/a",
        "requirement_denominator_complete": False,
        "requirement_denominator_reason": "empty",
    })

    assert reviews and reviews[0]["type"] == "deliver"
    assert out["human_decision"] == HumanDecision.REJECT
    assert out["delivery_reviewed"] is True


def test_stale_append_only_requirement_warning_does_not_override_current_complete_fact():
    """旧轮 degraded 是审计账；本轮结构化完整事实为真时，不能被陈旧字符串永久误拒。"""
    allow, reason = can_auto_accept_delivery({
        **_VERIFIED,
        "requirement_denominator_complete": True,
        "requirement_denominator_reason": "",
        "degraded_reasons": ["plan_coverage:skipped(disabled)"],
    })

    assert allow is True, reason


def test_current_complete_denominator_unblocks_l6_despite_stale_extract_warning():
    """L6 也必须读本轮事实，不能被旧轮分母 degraded 永久毒死。"""
    assert should_write_success({
        **_VERIFIED,
        "complexity": Complexity.MEDIUM,
        "requirement_denominator_complete": True,
        "requirement_denominator_reason": "",
        "degraded_reasons": [
            "requirements_extract:source_truncated",
            "plan_coverage:skipped(no_requirement_items)",
        ],
    }) is True


def test_current_complete_denominator_does_not_hide_operator_disabled_coverage():
    """结构化分母只能覆盖同源旧事实，不能洗掉运维显式关闭覆盖校验。"""
    assert should_write_success({
        **_VERIFIED,
        "complexity": Complexity.MEDIUM,
        "requirement_denominator_complete": True,
        "degraded_reasons": ["plan_coverage:skipped(disabled)"],
    }) is False


def test_current_incomplete_denominator_blocks_l6_without_degraded_text():
    """结构化事实本身就是闸；删除 degraded 文案也不能让残缺分母穿透。"""
    assert should_write_success({
        **_VERIFIED,
        "complexity": Complexity.MEDIUM,
        "requirement_denominator_complete": False,
        "requirement_denominator_reason": "below_expected_after_retries",
        "degraded_reasons": [],
    }) is False


def test_missing_requirement_denominator_fails_closed_everywhere():
    """plan-inject/旧后段 checkpoint 可缺键；缺席不得猜成完整。"""
    from swarm.brain.gates import delivery_requires_human_review

    state = {key: value for key, value in _VERIFIED.items()
             if not key.startswith("requirement_denominator_")}
    allow, reason = can_auto_accept_delivery(state)

    assert allow is False and "requirement_denominator_incomplete" in reason
    assert delivery_requires_human_review(state) is True
    assert should_write_success({**state, "complexity": Complexity.MEDIUM}) is False


@pytest.mark.parametrize("decision", [None, "", "bogus"])
def test_missing_or_invalid_delivery_decision_fails_closed(decision):
    state = {"human_decision": decision} if decision is not None else {}

    assert delivery_outcome(state) == "FAILED"
    assert after_deliver(state) == "learn_failure"


def test_runner_missing_delivery_decision_lands_failed_not_complete(monkeypatch):
    """跨层锁：即使绕过图路由直接进入 runner，缺失决策也不能宣布 DONE。"""
    import swarm.brain.runner as runner

    writes: list[dict] = []
    events: list[dict] = []

    async def _emit(_queue, event):
        events.append(event)

    monkeypatch.setattr(runner, "_emit", _emit)
    monkeypatch.setattr(runner, "_sync_task_from_state", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_sweep_unverified_footprints", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_emit_task_notification", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_failed_machine_account", lambda *_a, **_k: {})
    monkeypatch.setattr(runner, "audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runner.store, "get_task",
        lambda tid: {"id": tid, "project_id": "p", "description": "d"},
    )
    monkeypatch.setattr(
        runner.store, "update_task",
        lambda tid, **kw: writes.append(kw) or {"id": tid, **kw},
    )

    asyncio.run(runner._handle_post_run("t-missing-decision", {"l2_passed": True}, None))

    assert [row["status"] for row in writes if row.get("status")] == ["FAILED"]
    assert any(event.get("step") == "error" for event in events)
    assert not any(event.get("step") == "complete" for event in events)

    writes.clear()
    events.clear()
    asyncio.run(runner._handle_post_run("t-polluted-review", {
        **_VERIFIED,
        "auto_accept": True,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": "false",  # 污染字符串绝不等于已人审
        "requirement_denominator_complete": False,
        "requirement_denominator_reason": "empty",
    }, None))
    assert [row["status"] for row in writes if row.get("status")] == ["FAILED"]
    assert not any(event.get("step") == "complete" for event in events)


def test_explicit_manual_delivery_mode_is_not_overridden_by_environment(monkeypatch):
    """请求显式 auto_accept=False 时，即便进程默认=1，也必须进入人工 interrupt。"""
    seen: list[dict] = []

    def _interrupt(payload):
        seen.append(payload)
        return {"decision": "accept"}

    monkeypatch.setenv("SWARM_AUTO_ACCEPT", "1")
    monkeypatch.setattr(nodes, "interrupt", _interrupt)
    out = nodes.deliver({
        **_VERIFIED,
        "auto_accept": False,
        "task_id": "t-manual",
        "merged_diff": "diff --git a/a b/a",
        "requirement_denominator_complete": False,
        "requirement_denominator_reason": "empty",
    })

    assert seen and seen[0]["type"] == "deliver"
    assert out["human_decision"] == HumanDecision.ACCEPT


def test_explicit_manual_confirm_mode_is_not_overridden_by_environment(monkeypatch):
    """CONFIRM sibling 同样不得用进程 env 覆盖请求级 false。"""
    seen: list[dict] = []
    monkeypatch.setenv("SWARM_AUTO_ACCEPT", "1")
    monkeypatch.setattr(
        nodes,
        "interrupt",
        lambda payload: seen.append(payload) or {"decision": "accept"},
    )

    out = nodes.confirm_plan({
        "auto_accept": False,
        "plan_valid": True,
        "task_id": "t-manual-confirm",
        "task_description": "review me",
        "complexity": Complexity.MEDIUM,
    })

    assert seen and seen[0]["type"] == "confirm_plan"
    assert out["human_decision"] == HumanDecision.ACCEPT


def test_explicit_manual_mode_reaches_planning_interactions_despite_environment(monkeypatch):
    """澄清/方案评审 sibling 也只读 state，不能让 env 越过 runner 边界覆盖显式 false。"""
    from swarm.brain import planning_nodes
    from swarm.brain.nodes.shared import _planning_triage

    monkeypatch.setenv("SWARM_AUTO_ACCEPT", "1")

    assert planning_nodes._auto_mode({"auto_accept": False}) is False
    assert _planning_triage(
        "请设计一个包含多个模块、需要澄清边界的复杂系统",
        Complexity.COMPLEX,
        {"auto_accept": False},
    )["needs_clarify"] is True


def test_l3_without_published_changes_is_honestly_skipped(monkeypatch):
    import swarm.brain.l3_gitlab as gitlab

    calls: list[dict] = []
    monkeypatch.setattr(verify, "effective_complexity", lambda _s: Complexity.COMPLEX)
    monkeypatch.setattr(gitlab, "gitlab_configured", lambda: True)
    monkeypatch.setattr(gitlab, "l3_push_enabled", lambda: False)
    monkeypatch.setattr(
        gitlab,
        "trigger_and_poll_pipeline",
        lambda **kwargs: calls.append(kwargs) or (True, "green default branch"),
    )

    out = asyncio.run(verify.verify_l3({
        "task_id": "t-batch3",
        "project_id": "p-batch3",
        "merged_diff": "diff --git a/a b/a",
    }))

    assert calls == [], "未发布 merged_diff 时不得用默认 ref 的绿灯冒充本次 L3"
    assert out["l3_passed"] is None
    assert out["l3_skipped"] is True
    assert out["l3_skip_reason"] == "changes_not_published"
    assert "l3_skipped:changes_not_published" in out["degraded_reasons"]
    assert blocking_degraded_reasons(out["degraded_reasons"]), "未发布变更不得写入成功记忆"


def test_l3_published_ref_poll_error_never_falls_back_to_unbound_staging(monkeypatch):
    """GitLab 已发布本次精确分支后，轮询异常只能如实记未验证，不能换未绑定分支的后端。"""
    import swarm.brain.l3_gitlab as gitlab

    staging_calls: list[str] = []

    class _StagingLLM:
        async def ainvoke(self, _messages):
            staging_calls.append("llm")
            return type("Response", (), {
                "content": json.dumps({"l3_passed": True, "message": "staging green"})
            })()

    monkeypatch.setenv("SWARM_STAGING_URL", "https://staging.invalid")
    monkeypatch.setattr(verify, "effective_complexity", lambda _s: Complexity.COMPLEX)
    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: "/project")
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _StagingLLM())
    monkeypatch.setattr(gitlab, "gitlab_configured", lambda: True)
    monkeypatch.setattr(gitlab, "l3_push_enabled", lambda: True)
    monkeypatch.setattr(
        gitlab, "push_merged_diff_branch",
        lambda *_a, **_k: ("swarm/l3/exact-task", None),
    )
    monkeypatch.setattr(
        gitlab, "trigger_and_poll_pipeline",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("GitLab poll unavailable")),
    )

    out = asyncio.run(verify.verify_l3({
        "task_id": "exact-task",
        "project_id": "p-batch3",
        "merged_diff": "diff --git a/a b/a",
    }))

    assert staging_calls == [], "精确 GitLab ref 的验证异常后不得改用未绑定该 ref 的 staging/LLM"
    assert out["l3_passed"] is None
    assert out["l3_skipped"] is True
    assert out["l3_skip_reason"] == "published_ref_unverified"
    assert out["l3_branch"] == "", "未完成精确 ref 验证时不得把分支暴露给 MR 消费者"
    assert "l3_skipped:published_ref_unverified" in out["degraded_reasons"]


class _RequirementLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return type("Response", (), {"content": json.dumps(self.payload)})()


def test_requirement_low_yield_exhaustion_marks_denominator_incomplete(monkeypatch):
    """18K 源料连续只抽一条时，即便条目接地，也不能把残缺需求分母冒充完整。"""
    source = ("requirement alpha must be supported.\n" * 800)[:18_000] + "x"
    llm = _RequirementLLM({"items": [{
        "text": "support alpha",
        "kind": "functional",
        "source_quote": "requirement alpha must be supported",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert len(out["requirement_items"]) == 1
    assert out["requirement_denominator_complete"] is False
    assert out["requirement_denominator_reason"] == "below_expected_after_retries"
    assert "requirements_extract:insufficient_count=1<6" in out["degraded_reasons"]
    allow, reason = can_auto_accept_delivery({
        **_VERIFIED,
        **out,
        "complexity": Complexity.MEDIUM,
    })
    assert allow is False and "requirement_denominator_incomplete" in reason
    assert should_write_success({
        **_VERIFIED,
        **out,
        "complexity": Complexity.MEDIUM,
    }) is False


def test_grounded_item_cap_marks_denominator_incomplete(monkeypatch):
    """真实接地条目被容量阀截掉也是需求分母缺失，不能藏在 informational rejected 账里。"""
    rows = [f"requirement-{i:03d}-must-exist" for i in range(101)]
    source = "\n".join(rows)
    llm = _RequirementLLM({"items": [
        {"text": f"deliver {row}", "kind": "functional", "source_quote": row}
        for row in rows
    ]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert len(out["requirement_items"]) == 100
    assert out["requirement_denominator_complete"] is False
    assert out["requirement_denominator_reason"] == "grounded_items_truncated"
    assert should_write_success({
        **_VERIFIED,
        **out,
        "complexity": Complexity.MEDIUM,
    }) is False


@pytest.mark.parametrize(
    ("decision", "expected_route"),
    [
        ("accept", "learn_success"),
        ("reject", "learn_failure"),
    ],
)
def test_partial_human_decision_controls_learning_route(monkeypatch, decision, expected_route):
    """PARTIAL 自动模式先转人工；仅人工接受才交付并写 L2 partial，拒绝才写失败经验。"""
    monkeypatch.setattr(nodes, "interrupt", lambda _payload: {"decision": decision})
    partial_state = {
        "abandoned_subtask_ids": ["st-abandoned"],
        "plan": {"subtasks": [{"id": "st-done"}, {"id": "st-abandoned"}]},
        "subtask_results": {"st-done": {"l1_passed": True}},
    }
    source = {
        **_VERIFIED,
        **partial_state,
        "auto_accept": True,
        "task_id": "t-partial",
        "merged_diff": "diff --git a/a b/a",
    }
    delivered = nodes.deliver(source)
    terminal_state = {**source, **delivered}

    assert delivered["human_decision"] == HumanDecision(decision)
    assert after_deliver(terminal_state) == expected_route


def test_runner_uses_partial_outcome_for_accepted_zero_completed_remaining(monkeypatch):
    """人工已接受 PARTIAL 后，runner 不得用 completed_n=0 私判覆盖成 FAILED。"""
    import swarm.brain.runner as runner

    writes: list[dict] = []
    events: list[dict] = []

    async def _emit(_queue, event):
        events.append(event)

    monkeypatch.setattr(runner, "_emit", _emit)
    monkeypatch.setattr(runner, "_sync_task_from_state", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_sweep_unverified_footprints", lambda *_a, **_k: None)
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
        "auto_accept": True,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
        "dispatch_remaining": ["st-never-ran"],
        "subtask_results": {},
        "task_description": "d",
    }
    asyncio.run(runner._handle_post_run("t-partial", state, None))

    terminal_writes = [row["status"] for row in writes if row.get("status")]
    assert terminal_writes == ["FAILED"]
    assert not any(e.get("status") == "partial" for e in events)


def test_auto_discovered_apply_partial_is_not_announced_without_human_review():
    """auto ACCEPT 后才发现 apply 残缺时，不能绕过 DELIVER 人审落 PARTIAL。"""
    state = {
        **_VERIFIED,
        "auto_accept": True,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": False,
        "degraded_reasons": ["delivery_apply_failed"],
    }

    assert delivery_outcome(state) == "FAILED"


@pytest.mark.parametrize("partial_state", [
    {
        "abandoned_subtask_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {"st-good": {"l1_passed": True}},
    },
    {
        "partial_salvage_ids": ["st-bad"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-bad"}]},
        "subtask_results": {
            "st-good": {"l1_passed": True},
            "st-bad": {"l1_passed": False},
        },
    },
])
def test_partial_human_accept_runs_delivery_finalizer(monkeypatch, partial_state):
    """PARTIAL 经人工接受进入 learn_success 后，必须实际执行 apply/commit finalizer。"""
    delivered: list[tuple] = []

    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: "/project")

    async def _deliver(*args):
        delivered.append(args)
        return {
            "ap": {"ok": True, "applied": ["a.py"], "failed": []},
            "out_files": ["a.py"],
            "wm": {},
            "commit": {"ok": True, "committed": True, "commit_hash": "abc"},
        }

    async def _persist(_state, _parsed):
        return {"persisted": False, "reason": "test"}

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _deliver)
    monkeypatch.setattr("swarm.brain.learn_store.persist_learn_success", _persist)
    monkeypatch.setattr(
        "swarm.knowledge.hooks.schedule_incremental_update", lambda *_a, **_k: None,
    )

    state = {
        "task_id": "t-partial",
        "project_id": "p-partial",
        "task_description": "partial accepted",
        "complexity": Complexity.SIMPLE,
        "merged_diff": "diff --git a/a.py b/a.py\n+new\n",
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
        **_VERIFIED,
        **partial_state,
    }
    asyncio.run(nodes.learn_success(state))

    assert delivered, "人工接受的部分交付必须进入 apply/commit finalizer"


def test_success_memory_writer_rejects_nonaccepted_outcome(monkeypatch):
    """双保险：即使路由误把 REJECT 送到成功写入器，也不得落 L2 success/L6。"""
    from swarm.brain import learn_store

    class _MustNotOpenStore:
        def __init__(self):  # pragma: no cover - 调用即表示绕过终态闸
            raise AssertionError("non-accepted outcome must stop before MemoryStore")

    monkeypatch.setattr(learn_store, "MemoryStore", _MustNotOpenStore)
    out = asyncio.run(learn_store.persist_learn_success({
        "project_id": "p-batch3",
        "task_id": "t-rejected",
        "human_decision": HumanDecision.REJECT,
    }, {"pattern_name": "must-not-write"}))

    assert out == {"persisted": False, "reason": "invalid_success_outcome:failed"}


@pytest.mark.parametrize("delivery_reviewed", [False, True])
def test_apply_failure_uses_failure_learning_not_success_learning(
        monkeypatch, delivery_reviewed):
    """apply 最终失败是失败经验；即使之前人工接受 PARTIAL 也不能写成功/L6。"""
    success_writes: list[dict] = []
    failure_writes: list[dict] = []

    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: "/project")

    async def _deliver(*_args):
        return {
            "ap": {"ok": False, "applied": [], "failed": ["a.py"]},
            "out_files": [],
            "wm": {},
            "commit": {"ok": False, "committed": False, "reason": "apply failed"},
        }

    async def _persist_success(state, _parsed):
        success_writes.append(dict(state))
        return {"persisted": True}

    async def _persist_failure(state, _parsed):
        failure_writes.append(dict(state))
        return {"persisted": True}

    class _FailureLLM:
        async def ainvoke(self, _messages):
            return type("Response", (), {"content": json.dumps({
                "mistake_name": "delivery-apply-failed",
                "mistake_description": "交付落盘失败",
                "root_cause": "patch conflict",
            })})()

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _deliver)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FailureLLM())
    monkeypatch.setattr("swarm.brain.learn_store.persist_learn_success", _persist_success)
    monkeypatch.setattr("swarm.brain.learn_store.persist_learn_failure", _persist_failure)

    out = asyncio.run(nodes.learn_success({
        **_VERIFIED,
        "auto_accept": not delivery_reviewed,
        "task_id": "t-apply-failed",
        "project_id": "p",
        "task_description": "deliver change",
        "complexity": Complexity.SIMPLE,
        "merged_diff": "diff --git a/a.py b/a.py\n+x\n",
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": delivery_reviewed,
        **({
            "abandoned_subtask_ids": ["st-missing"],
            "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-missing"}]},
            "subtask_results": {"st-good": {"l1_passed": True}},
        } if delivery_reviewed else {}),
    }))

    assert success_writes == []
    assert len(failure_writes) == 1
    assert "delivery_apply_failed" in failure_writes[0]["degraded_reasons"]
    assert out["learned"] is False
    assert out["delivery_finalization_failed"] is True
    assert "delivery_apply_failed" in out["degraded_reasons"]


def test_apply_rollback_failure_files_reach_failure_learning_state(monkeypatch):
    """回滚失败文件必须进入 checkpoint 可持久化账，不能停在 helper 返回值/日志。"""
    failure_writes: list[dict] = []
    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: "/project")

    async def _deliver(*_args):
        return {
            "ap": {
                "ok": False,
                "stage": "apply_partial_rollback_failed",
                "applied": ["a.py"],
                "failed": [{"files": ["b.py"], "stage": "check"}],
                "rollback_failed": ["a.py"],
            },
            "out_files": ["a.py", "b.py"],
            "wm": {},
            "commit": {},
        }

    async def _persist_failure(state, _parsed):
        failure_writes.append(dict(state))
        return {"persisted": True}

    class _FailureLLM:
        async def ainvoke(self, _messages):
            return type("Response", (), {"content": "{}"})()

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _deliver)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FailureLLM())
    monkeypatch.setattr(
        "swarm.brain.learn_store.persist_learn_failure", _persist_failure,
    )

    out = asyncio.run(nodes.learn_success({
        **_VERIFIED,
        "task_id": "t-rollback-failed",
        "project_id": "p",
        "task_description": "deliver change",
        "complexity": Complexity.SIMPLE,
        "merged_diff": "diff --git a/a.py b/a.py\n+x\n",
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }))

    expected = "delivery_rollback_failed:a.py"
    assert len(failure_writes) == 1
    assert expected in failure_writes[0]["degraded_reasons"]
    assert expected in out["degraded_reasons"]
    assert out["delivery_finalization_failed"] is True


@pytest.mark.parametrize("failure_mode", ["missing_project_path", "finalizer_raises"])
def test_unavailable_delivery_finalization_is_failure_learning(monkeypatch, failure_mode):
    """无项目路径或 finalizer 异常都意味着没能交付，不能继续成功/L2/DONE。"""
    success_writes: list[dict] = []
    failure_writes: list[dict] = []
    monkeypatch.setattr(
        nodes,
        "_get_project_path",
        lambda _pid: "" if failure_mode == "missing_project_path" else "/project",
    )

    async def _deliver(*_args):
        raise OSError("disk unavailable")

    async def _persist_success(state, _parsed):
        success_writes.append(dict(state))
        return {"persisted": True}

    async def _persist_failure(state, _parsed):
        failure_writes.append(dict(state))
        return {"persisted": True}

    class _FailureLLM:
        async def ainvoke(self, _messages):
            return type("Response", (), {"content": "{}"})()

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _deliver)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FailureLLM())
    monkeypatch.setattr("swarm.brain.learn_store.persist_learn_success", _persist_success)
    monkeypatch.setattr("swarm.brain.learn_store.persist_learn_failure", _persist_failure)

    state = {
        **_VERIFIED,
        "task_id": "t-finalizer-unavailable",
        "project_id": "p",
        "task_description": "deliver change",
        "complexity": Complexity.SIMPLE,
        "merged_diff": "diff --git a/a.py b/a.py\n+x\n",
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
    }
    out = asyncio.run(nodes.learn_success(state))
    terminal = {**state, **out}

    assert success_writes == []
    assert len(failure_writes) == 1
    assert out["learned"] is False
    assert out["delivery_finalization_failed"] is True
    assert delivery_outcome(terminal) == "FAILED"


@pytest.mark.parametrize(
    "impl_result",
    [
        {"l3_passed": None, "l3_skipped": True, "l3_skip_reason": "changes_not_published"},
        {"l3_passed": False, "l3_skipped": False, "l3_skip_reason": "push_failed"},
    ],
)
def test_l3_non_success_always_clears_stale_branch(monkeypatch, impl_result):
    """LangGraph 字典合并不能让上一轮 l3_branch 污染本轮失败/跳过出口。"""
    async def _impl(_state):
        return dict(impl_result)

    monkeypatch.setattr(verify, "_verify_l3_impl", _impl)
    old_state = {"l3_branch": "swarm/l3/old-task"}
    patch = asyncio.run(verify.verify_l3(old_state))
    merged = {**old_state, **patch}

    assert patch["l3_branch"] == ""
    assert merged["l3_branch"] == ""
