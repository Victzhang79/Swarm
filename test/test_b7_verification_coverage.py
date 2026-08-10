"""B-7/V-C3（27 号文 §6.3 原则 3）：`verification_coverage` 验证覆盖账。

治前：四闸（l2/runtime_smoke/l3）全 None（都没验）仍 auto_accept 放行，"验了没有"
只能靠人读 degraded_reasons 散文分辨。治法：每道验证闸写一格（浅合并 reducer，
各闸各写各格，重入覆写同格），消费者=deliver payload 明示 + gates 拒因文案。

本文件锁：①reducer 合并语义；②三个 producer 薄包装格推导（含 unsupported 族→
unsupported_stack:<栈> 格）；③★B-7 接线修复★ test_cmd 通过出口不再吞
verification_unsupported_stack 族留痕（治前该支路 gates 拒 auto_accept 臂不可达）；
④deliver payload 明示；⑤gates 拒因带覆盖账。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.brain.nodes import verify as vmod
from swarm.brain.state import _merge_verification_coverage


def _run(coro):
    return asyncio.run(coro)


# ── ① reducer ────────────────────────────────────────────────────────────

def test_reducer_merges_cells_across_nodes_and_overwrites_same_cell():
    """各闸各写各格（浅合并）；同格覆写=重入取最新轮；None 容错。"""
    a = _merge_verification_coverage(None, {"l2": "passed"})
    assert a == {"l2": "passed"}
    b = _merge_verification_coverage(a, {"runtime_smoke": "skipped"})
    assert b == {"l2": "passed", "runtime_smoke": "skipped"}, \
        "last-write-wins 会把先写者的格整表抹掉（这正是要 reducer 的原因）"
    c = _merge_verification_coverage(b, {"l2": "failed"})
    assert c["l2"] == "failed" and c["runtime_smoke"] == "skipped"
    assert _merge_verification_coverage(None, None) == {}


# ── ② producer 薄包装 ─────────────────────────────────────────────────────

def test_verify_l2_wrapper_cell_unsupported_stack():
    """实现体携 verification_unsupported_stack 族 → l2 格=unsupported_stack:<栈>。"""
    async def _fake_impl(state, handoff):
        return {"l2_passed": True,
                "degraded_reasons": ["verification_unsupported_stack:php:l2"]}
    with patch.object(vmod, "_verify_l2_impl", side_effect=_fake_impl):
        out = _run(vmod.verify_l2({"task_id": "t"}))
    assert out["verification_coverage"] == {"l2": "unsupported_stack:php"}, out


def test_verify_l2_wrapper_cell_passed_and_failed():
    async def _ok(state, handoff):
        return {"l2_passed": True}

    async def _bad(state, handoff):
        return {"l2_passed": False}
    with patch.object(vmod, "_verify_l2_impl", side_effect=_ok):
        assert _run(vmod.verify_l2({}))["verification_coverage"] == {"l2": "passed"}
    with patch.object(vmod, "_verify_l2_impl", side_effect=_bad):
        assert _run(vmod.verify_l2({}))["verification_coverage"] == {"l2": "failed"}


def test_verify_l3_wrapper_cell_from_three_state():
    async def _skipped(state):
        return {"l3_passed": None, "l3_skipped": True}

    async def _passed(state):
        return {"l3_passed": True}
    with patch.object(vmod, "_verify_l3_impl", side_effect=_skipped):
        assert _run(vmod.verify_l3({}))["verification_coverage"] == {"l3": "skipped"}
    with patch.object(vmod, "_verify_l3_impl", side_effect=_passed):
        assert _run(vmod.verify_l3({}))["verification_coverage"] == {"l3": "passed"}


def test_verify_runtime_wrapper_cell_from_three_state():
    async def _failed(state):
        return {"runtime_smoke_passed": False}

    async def _skipped(state):
        return {"runtime_smoke_passed": None, "runtime_smoke_skipped": True}
    with patch.object(vmod, "_verify_runtime_impl", side_effect=_failed):
        assert _run(vmod.verify_runtime({}))["verification_coverage"] == {
            "runtime_smoke": "failed"}
    with patch.object(vmod, "_verify_runtime_impl", side_effect=_skipped):
        assert _run(vmod.verify_runtime({}))["verification_coverage"] == {
            "runtime_smoke": "skipped"}


def test_verify_l3_real_node_writes_skipped_cell():
    """真节点端到端（SIMPLE 复杂度跳过臂）：格必须真落（非只测包装桩）。"""
    out = _run(vmod.verify_l3({"complexity": "SIMPLE", "merged_diff": "",
                               "task_description": "d"}))
    # F3：格值分档 skipped:<reason>（B7-k 先例）——哪种跳过机读可辨
    assert out["verification_coverage"] == {"l3": "skipped:complexity_skip"}, out


# ── ③ B-7 接线修复：test_cmd 通过出口不再吞未支持族留痕 ─────────────────────

def test_unsupported_family_carried_on_test_cmd_pass_exit():
    """★接线修复锁★ 未支持栈 + 有 test_cmd + 沙箱测试通过：治前该出口【不携】
    degraded → gates 拒 auto_accept 臂整段不可达（闸造好了接在没人走的路上）。
    本条走真 `_verify_l2_impl`（只打桩外部协作面），出口必须携族留痕。"""
    state = {
        "task_id": "t", "project_id": "p", "complexity": "COMPLEX",
        "merged_diff": "diff --git a/x b/x\n+y\n",
        "task_description": "d", "acceptance_criteria": ["pytest"],
        "subtask_results": {}, "plan": None, "base_commit": "HEAD",
    }
    with patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch.object(vmod, "_l2_test_command_from_criteria", return_value="pytest"), \
         patch.object(vmod, "_stub_fingerprint_owner_ids", return_value=[]), \
         patch("swarm.brain.integration_review.run_integration_review",
               return_value=(True, [], {"compile_gate_unsupported_stack": "php"})), \
         patch("swarm.brain.nodes._try_l2_sandbox_verify", return_value=True):
        out = _run(vmod._verify_l2_impl(state, []))
    assert out.get("l2_passed") is True
    dr = out.get("degraded_reasons") or []
    assert any(r.startswith("verification_unsupported_stack:php") for r in dr), \
        f"test_cmd 通过出口吞了未支持族留痕（B-7 接线修复被回退）: {dr}"


def test_verify_l2_end_to_end_cell_on_unsupported_pass():
    """包装+实现体端到端：未支持栈测试通过 → l2 格=unsupported_stack:php。"""
    state = {
        "task_id": "t", "project_id": "p", "complexity": "COMPLEX",
        "merged_diff": "diff --git a/x b/x\n+y\n",
        "task_description": "d", "acceptance_criteria": ["pytest"],
        "subtask_results": {}, "plan": None, "base_commit": "HEAD",
    }
    with patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch.object(vmod, "_l2_test_command_from_criteria", return_value="pytest"), \
         patch.object(vmod, "_stub_fingerprint_owner_ids", return_value=[]), \
         patch("swarm.brain.integration_review.run_integration_review",
               return_value=(True, [], {"compile_gate_unsupported_stack": "php"})), \
         patch("swarm.brain.nodes._try_l2_sandbox_verify", return_value=True):
        out = _run(vmod.verify_l2(state))
    assert out["verification_coverage"] == {"l2": "unsupported_stack:php"}, out


# ── ④ deliver payload 明示 ────────────────────────────────────────────────

def test_deliver_payload_surfaces_coverage():
    from swarm.brain.runner import _build_result_payload
    payload = _build_result_payload(
        {"l2_passed": True,
         "verification_coverage": {"l2": "unsupported_stack:php",
                                   "runtime_smoke": "skipped"}})
    assert payload["verification_coverage"]["l2"] == "unsupported_stack:php", payload


def test_deliver_payload_omits_empty_coverage():
    """旧 checkpoint（无覆盖账）不得冒出空壳键。"""
    from swarm.brain.runner import _build_result_payload
    payload = _build_result_payload({"l2_passed": True})
    assert "verification_coverage" not in payload


# ── ⑤ gates 拒因带覆盖账 ──────────────────────────────────────────────────

def test_gates_reason_includes_coverage_ledger():
    from swarm.brain.gates import can_auto_accept_delivery
    allow, reason = can_auto_accept_delivery({
        "l2_passed": True, "failed_subtask_ids": [],
        "degraded_reasons": ["verification_unsupported_stack:php:l2"],
        "verification_coverage": {"l2": "unsupported_stack:php",
                                  "runtime_smoke": "skipped"},
    })
    assert allow is False
    assert "l2=unsupported_stack:php" in reason, reason
    assert "runtime_smoke=skipped" in reason, reason


# ── ⑥ R2 hunter M-1：passed 必须分出「放行但未验」档 ─────────────────────────

def test_verify_l2_wrapper_cell_passed_unverified_tiering():
    """★R2 hunter M-1★ l2_passed=True 但 degraded 携「未真正验证」三族（无测试命令 /
    测试降级 LLM 判定 / 编译未核验）→ 格必须记 passed:unverified，绝不谎称 passed。"""
    for deg in (["l2_no_test_executed"], ["l2_test_downgraded_to_llm"],
                ["l2_compile_unverified:no_project_path"]):
        async def _fake(state, handoff, _d=deg):
            return {"l2_passed": True, "degraded_reasons": list(_d)}
        with patch.object(vmod, "_verify_l2_impl", side_effect=_fake):
            out = _run(vmod.verify_l2({}))
        assert out["verification_coverage"] == {"l2": "passed:unverified"}, (deg, out)
    # 对照面：与「未验证」无关的 degraded 不沾染本档（passed 仍为真验过）
    async def _clean(state, handoff):
        return {"l2_passed": True, "degraded_reasons": ["delivery_apply_incomplete"]}
    with patch.object(vmod, "_verify_l2_impl", side_effect=_clean):
        assert _run(vmod.verify_l2({}))["verification_coverage"] == {"l2": "passed"}


def test_passed_unverified_cell_does_not_trip_unsupported_gate():
    """passed:unverified 是观测档不是硬闸——这三族本来就不拦 auto_accept（拦 L6 走
    should_write_success 的 degraded 通道），格值绝不误触发未支持栈臂。"""
    from swarm.brain.gates import can_auto_accept_delivery
    allow, _ = can_auto_accept_delivery({
        "l2_passed": True, "failed_subtask_ids": [],
        "verification_coverage": {"l2": "passed:unverified"},
    })
    assert allow is True


# ── ⑦ R2 hunter H-1：覆盖账是本轮事实，degraded 粘滞条目不误拦 ────────────────

def test_gates_prefers_current_round_cell_over_stale_degraded():
    """★R2 hunter H-1★ degraded_reasons 是 append-only 审计账：replan 后栈已变 /
    升级后该栈已支持时，旧轮 unsupported 条目永久粘滞。覆盖账在场即以格为准——
    本轮 l2 格=passed 时旧条目不得再拦（否则「历史某轮没验」误拦成「本轮没验」）。"""
    from swarm.brain.gates import can_auto_accept_delivery
    allow, reason = can_auto_accept_delivery({
        "l2_passed": True, "failed_subtask_ids": [],
        "degraded_reasons": ["verification_unsupported_stack:php:l2"],  # 旧轮粘滞
        "verification_coverage": {"l2": "passed"},  # 本轮真验过
    })
    assert allow is True, f"旧轮粘滞条目误拦本轮: {reason}"


def test_gates_falls_back_to_degraded_scan_on_legacy_checkpoint():
    """旧 checkpoint 无覆盖账 → 回退 degraded 扫描（含旧前缀），硬闸兼容存量不失效。"""
    from swarm.brain.gates import can_auto_accept_delivery
    for legacy in (["verification_unsupported_stack:php:l2"],
                   ["l2_unsupported_stack:php"]):
        allow, reason = can_auto_accept_delivery({
            "l2_passed": True, "failed_subtask_ids": [],
            "degraded_reasons": legacy,
        })
        assert allow is False, f"旧 checkpoint 硬拦被拆: {legacy}"
        assert "unsupported_stack" in reason


def test_gates_blocks_on_cell_alone_without_degraded_entry():
    """覆盖账格是唯一证据（degraded 无族条目）时同样拦——格推导与族留痕同源，
    任一通道可见即够。"""
    from swarm.brain.gates import can_auto_accept_delivery
    allow, reason = can_auto_accept_delivery({
        "l2_passed": True, "failed_subtask_ids": [],
        "verification_coverage": {"l2": "unsupported_stack:ruby"},
    })
    assert allow is False
    assert "ruby" in reason


# ── ⑧ R2 reviewer HIGH：infra_degrade 出口不吞族留痕 ─────────────────────────

def test_unsupported_family_carried_on_infra_degrade_exit():
    """★R2 reviewer HIGH★ 未支持栈与 infra 降级（compile_unverified）【可同现】：
    治前该出口吞 _l2_unverified_degraded → 族事实静默丢失、覆盖账被误推成 failed。
    走真 `_verify_l2_impl`（只打桩外部协作面）。"""
    state = {
        "task_id": "t", "project_id": "p", "complexity": "COMPLEX",
        "merged_diff": "diff --git a/x b/x\n+y\n",
        "task_description": "d", "acceptance_criteria": ["pytest"],
        "subtask_results": {}, "plan": None, "base_commit": "HEAD",
    }
    with patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch.object(vmod, "_l2_test_command_from_criteria", return_value="pytest"), \
         patch.object(vmod, "_stub_fingerprint_owner_ids", return_value=[]), \
         patch("swarm.brain.integration_review.run_integration_review",
               return_value=(False, ["compile failed"],
                             {"compile_gate_unsupported_stack": "php",
                              "compile_unverified": True})):
        out = _run(vmod._verify_l2_impl(state, []))
    assert out.get("l2_passed") is False
    dr = out.get("degraded_reasons") or []
    assert any(r.startswith("verification_unsupported_stack:php") for r in dr), \
        f"infra_degrade 出口吞了未支持族留痕（R2 reviewer HIGH 修复被回退）: {dr}"
    assert out["l2_details"]["infra_degrade"] == "compile_unverified", \
        "族留痕不得改动 infra 降级归因"


def test_infra_degrade_exit_cell_is_unsupported_not_failed():
    """同上路径端到端过包装：族在 degraded 增量里 → 格=unsupported_stack:php
    （格推导先认族再看 l2_passed），『栈没实现』不被『failed』掩盖。"""
    state = {
        "task_id": "t", "project_id": "p", "complexity": "COMPLEX",
        "merged_diff": "diff --git a/x b/x\n+y\n",
        "task_description": "d", "acceptance_criteria": ["pytest"],
        "subtask_results": {}, "plan": None, "base_commit": "HEAD",
    }
    with patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch.object(vmod, "_l2_test_command_from_criteria", return_value="pytest"), \
         patch.object(vmod, "_stub_fingerprint_owner_ids", return_value=[]), \
         patch("swarm.brain.integration_review.run_integration_review",
               return_value=(False, ["compile failed"],
                             {"compile_gate_unsupported_stack": "php",
                              "compile_unverified": True})):
        out = _run(vmod.verify_l2(state))
    assert out["verification_coverage"] == {"l2": "unsupported_stack:php"}, out


# ── ⑨ R2 reviewer MEDIUM：本地通过出口与沙箱通过出口对称 ──────────────────────

def test_unsupported_family_carried_on_local_pass_exit():
    """★R2 reviewer MEDIUM★ 沙箱不可用（None）→ 本地工具链测试通过：与沙箱通过
    出口对称，族留痕同样不丢（只锁沙箱出口会让本地出口回退无测试会红）。"""
    state = {
        "task_id": "t", "project_id": "p", "complexity": "COMPLEX",
        "merged_diff": "diff --git a/x b/x\n+y\n",
        "task_description": "d", "acceptance_criteria": ["pytest"],
        "subtask_results": {}, "plan": None, "base_commit": "HEAD",
    }
    with patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), \
         patch.object(vmod, "_l2_test_command_from_criteria", return_value="pytest"), \
         patch.object(vmod, "_stub_fingerprint_owner_ids", return_value=[]), \
         patch("swarm.brain.integration_review.run_integration_review",
               return_value=(True, [], {"compile_gate_unsupported_stack": "php"})), \
         patch("swarm.brain.nodes._try_l2_sandbox_verify", return_value=None), \
         patch("swarm.brain.nodes._try_l2_local_verify", return_value=True):
        out = _run(vmod._verify_l2_impl(state, []))
    assert out.get("l2_passed") is True
    dr = out.get("degraded_reasons") or []
    assert any(r.startswith("verification_unsupported_stack:php") for r in dr), \
        f"本地通过出口吞了未支持族留痕: {dr}"
