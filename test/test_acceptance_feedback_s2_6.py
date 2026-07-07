"""S2-6 验收失败回灌 + gates acceptance 三态 + deliver 人工闸 payload 补全 —— 行为测试。

覆盖面（ACCEPTANCE_DESIGN §3/§4 + task#27 指引）：
1. failure.py：classification=acceptance_failed 经 runtime_smoke 专类分支——归因命中 →
   定向恢复 + retry_guidance 带"验收断言失败"定性与逐条断言 verdict 证据；归因不出 →
   replan 共用 replan_count；双闸（replan_count / targeted_recovery_counts）有界回归。
2. shared.runtime_failure_evidence：acceptance 前缀证据键并入提取（镜像 migration F3
   前缀契约），infra 噪声仍被排除。
3. gates.can_auto_accept_delivery：acceptance_passed 三态——仅显式 False 阻断（专类文案
   "acceptance_failed:"），None/True/缺键不阻断；判序与 l2/runtime/verification_failure 共存。
4. deliver 人工闸 payload：runtime_smoke/migration_verify/acceptance/coverage/
   degraded_reasons 全键呈现；旧 checkpoint 缺键容错；断言 verdict 限量+截断。
5. LEARN 双闸：acceptance_passed=False 轮 / skipped(None)+degraded 轮均不写 L6 成功模式。
"""
import asyncio

from swarm.brain.gates import can_auto_accept_delivery
from swarm.brain.nodes import handle_failure
from swarm.brain.nodes.shared import runtime_failure_evidence
from swarm.types import Complexity, FileScope, SubTask, TaskPlan, WorkerOutput


def _st(sid, *, create=None, writable=None, covers=None):
    return SubTask(
        id=sid,
        description=f"subtask {sid}",
        scope=FileScope(writable=writable or [], create_files=create or []),
        covers=covers or [],
    )


def _plan(*subtasks):
    return TaskPlan(subtasks=list(subtasks))


def _wo(sid, l1_passed=True):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if l1_passed else "",
        summary="",
        l1_passed=l1_passed,
        l1_details={},
        confidence="high" if l1_passed else "low",
    )


# 断言失败证据（verify.py _acceptance_evidence_keys 的真实形状：请求路径 + body 头部——
# 500 错误 body 常含栈帧文件名，是归因命中路由文件写者的现实通道）
_ACCEPT_EVIDENCE = (
    "[a-1] req=req-11aa22bb GET /api/users → 期待 status∈[200] 实得 404；"
    "实得 http_code=404；body 头部: Not Found\n"
    "[a-2] req=req-33cc44dd POST /api/users → 期待 status∈[200, 201] 实得 500；"
    "实得 http_code=500；body 头部: NullPointerException at app.UsersController.create"
    "(UsersController.java:42)"
)
_ACCEPT_FAILURES = [
    {"id": "a-1", "req_id": "req-11aa22bb",
     "request": {"method": "GET", "path": "/api/users"},
     "verdict": "fail", "http_code": 404, "reason": "期待 status∈[200] 实得 404",
     "body_excerpt": "Not Found"},
    {"id": "a-2", "req_id": "req-33cc44dd",
     "request": {"method": "POST", "path": "/api/users"},
     "verdict": "fail", "http_code": 500, "reason": "期待 status∈[200, 201] 实得 500",
     "body_excerpt": "NullPointerException at app.UsersController.create"
                     "(UsersController.java:42)"},
]


def _acceptance_details(**over):
    d = {
        "classification": "acceptance_failed",
        "log_tail": "application started on port 8080",
        "acceptance_evidence": _ACCEPT_EVIDENCE,
        "acceptance_failed_count": 2,
        "acceptance_failures": [dict(r) for r in _ACCEPT_FAILURES],
    }
    d.update(over)
    return d


def _runtime_state(details, **over):
    """acceptance_failed 轮的最小 state：st-1 写 UsersController（证据可命中），st-2 无关。"""
    state = {
        "verification_failure": "runtime_smoke",
        "runtime_smoke_passed": False,
        "runtime_smoke_details": details,
        "acceptance_passed": False,
        "failed_subtask_ids": [],
        "subtask_results": {"st-1": _wo("st-1"), "st-2": _wo("st-2")},
        "plan": _plan(
            _st("st-1", create=["src/app/UsersController.java"]),
            _st("st-2", create=["src/app/Other.java"]),
        ),
        "dispatch_remaining": [],
        "replan_count": 0,
        "subtask_retry_counts": {},
        "targeted_recovery_counts": {},
    }
    state.update(over)
    return state


def _run(state):
    return asyncio.run(handle_failure(state))


# ═════════════ 1. 证据提取：acceptance 前缀键并入（镜像 migration F3） ═════════════

def test_runtime_failure_evidence_consumes_acceptance_prefix_keys():
    blob = runtime_failure_evidence(_acceptance_details())
    assert "/api/users" in blob, "断言请求路径必须进证据面（路径能命中路由文件写者）"
    assert "UsersController.java" in blob, "body 头部的栈帧文件名必须进证据面"
    assert "期待 status∈[200] 实得 404" in blob, "逐条断言 verdict 文本必须进证据面"


def test_runtime_failure_evidence_acceptance_still_excludes_infra_noise():
    details = _acceptance_details(
        derivation_evidence={"config": "src/main/resources/application.yml"},
        sandbox={"rebuild_output": "built src/app/Unrelated.java"},
    )
    blob = runtime_failure_evidence(details)
    assert "application.yml" not in blob, "基础设施留痕绝不进证据面（每轮误归因源）"
    assert "Unrelated.java" not in blob


# ═════════════ 1b. F4：acceptance_failed 时健康启动 log_tail 不进证据面 ═════════════

def test_runtime_failure_evidence_acceptance_excludes_healthy_log_tail():
    """F4：断言失败时探活已过、应用健康启动——log_tail 是纯噪声，其中打印的配置
    文件名会把无辜写者每轮定向重派。acceptance/migration 前缀族 + code_error_hits 照收。"""
    details = _acceptance_details(
        log_tail="INFO loading config src/main/resources/application.yml — started OK",
        code_error_hits=["hit: src/app/UsersController.java"],
    )
    blob = runtime_failure_evidence(details)
    assert "application.yml" not in blob, "健康启动日志绝不进 acceptance_failed 证据面"
    assert "started OK" not in blob
    assert "/api/users" in blob, "acceptance 前缀族照收"
    assert "hit: src/app/UsersController.java" in blob, "code_error_hits 照收"


def test_runtime_failure_evidence_code_error_log_tail_still_included_regression():
    """F4 回归：非 acceptance 分类（code_error 等）log_tail 是失败现场，照收不变。"""
    blob = runtime_failure_evidence({
        "classification": "code_error",
        "log_tail": "Exception at src/app/Boot.java:7",
    })
    assert "src/app/Boot.java" in blob


def test_acceptance_failed_healthy_log_tail_never_attributes_config_writer():
    """F4 端到端：健康日志里的配置文件名不得把其写者归因成失败源；真证据（断言
    body 栈帧）命中的写者照常定向。"""
    state = _runtime_state(_acceptance_details(
        log_tail="INFO using config src/main/resources/application.yml — started"))
    state["plan"] = _plan(
        _st("st-1", create=["src/app/UsersController.java"]),
        _st("st-2", create=["src/main/resources/application.yml"]),
    )
    out = _run(state)
    assert out["failure_strategy"] == "retry"
    assert "st-1" in out["dispatch_remaining"], "真证据命中的写者照常定向"
    assert "st-2" not in out["dispatch_remaining"], "健康日志提及的配置写者绝不连坐"


# ═════════════ 2. failure.py：acceptance_failed 归因/回灌 ═════════════

def test_acceptance_failed_attribution_hit_targeted_with_specific_guidance():
    out = _run(_runtime_state(_acceptance_details()))

    # 定向恢复形态（与 runtime 分支同构）：只重做归因子任务，保留成功兄弟
    assert out["failure_strategy"] == "retry"
    assert "st-1" in out["dispatch_remaining"]
    assert "st-2" not in out["dispatch_remaining"]
    assert "st-2" in out["subtask_results"], "成功兄弟不可被清空"
    assert out["targeted_recovery"] is True
    assert out["verification_failure"] is None, "清专类，不粘滞下一轮"
    assert "l2_passed" not in out, "绝不落 l2 分支（8bec098 专类教训）"

    # retry_guidance：acceptance_failed 专类定性 + 逐条断言 verdict 证据
    by_id = {s.id: s for s in out["plan"].subtasks}
    guidance = by_id["st-1"].retry_guidance
    assert "验收断言失败" in guidance, "定性文案必须区分于启动失败"
    assert "应用已启动" in guidance, "机制说明：应用已启动但接口行为不符预期"
    assert "/api/users" in guidance, "断言请求路径必须注入 worker 可见证据"
    assert "404" in guidance, "实得 http_code 必须注入"
    assert "启动失败根因" not in guidance, "不得沿用 runtime 启动失败的误导文案"
    assert not by_id["st-2"].retry_guidance, "未归因的兄弟不得被注入证据"

    # 有界记账：与 L2/runtime 共用 replan_count + 按子任务 targeted_recovery_counts
    assert out["replan_count"] == 1
    assert out["targeted_recovery_counts"] == {"st-1": 1}


def test_acceptance_failed_attribution_miss_falls_to_replan_shared_counter():
    details = _acceptance_details(
        acceptance_evidence="[a-1] req=req-x GET /health → 期待 200 实得 503；body 头部: ",
        acceptance_failures=[{"id": "a-1", "req_id": "req-x", "verdict": "fail",
                              "request": {"method": "GET", "path": "/health"},
                              "http_code": 503, "reason": "期待 200 实得 503"}],
    )
    out = _run(_runtime_state(details))
    assert out["failure_strategy"] == "replan"
    assert out["replan_count"] == 1, "归因不出退 replan 阶梯，与 L2 共用计数器"
    assert out["verification_failure"] is None
    assert "l2_passed" not in out


def test_acceptance_failed_replan_circuit_exhausted_escalates():
    out = _run(_runtime_state(_acceptance_details(), replan_count=99))
    assert out["failure_strategy"] == "escalate"
    assert out["failure_escalated"] is True
    assert out["verification_failure"] is None


def test_acceptance_failed_targeted_quota_exhausted_escalates():
    out = _run(_runtime_state(_acceptance_details(),
                              targeted_recovery_counts={"st-1": 99}))
    assert out["failure_strategy"] == "escalate"
    assert out["failure_escalated"] is True


def test_runtime_code_error_guidance_unchanged_regression():
    # 非 acceptance 分类仍走"运行时启动失败"文案（S1-6 语义不破坏）
    out = _run(_runtime_state({
        "classification": "code_error",
        "log_tail": "Exception at app.UsersController.init(UsersController.java:7)",
    }))
    assert out["failure_strategy"] == "retry"
    by_id = {s.id: s for s in out["plan"].subtasks}
    guidance = by_id["st-1"].retry_guidance
    assert "运行时启动失败" in guidance
    assert "验收断言失败" not in guidance


# ═════════════ 3. gates：acceptance 三态 ═════════════

_GATE_BASE = {"l2_passed": True}


def test_gate_acceptance_failed_blocks_with_specific_reason():
    allow, reason = can_auto_accept_delivery({**_GATE_BASE, "acceptance_passed": False})
    assert allow is False
    assert reason.startswith("acceptance_failed"), "专类文案，不得冒充 l2/l3/runtime"


def test_gate_acceptance_skipped_passed_or_missing_does_not_block():
    for val in (None, True):
        allow, reason = can_auto_accept_delivery(
            {**_GATE_BASE, "acceptance_passed": val})
        assert allow is True, f"acceptance_passed={val} 不得阻断: {reason}"
    allow, _ = can_auto_accept_delivery(dict(_GATE_BASE))
    assert allow is True, "旧 checkpoint 缺键不得阻断"


def test_gate_runtime_false_acceptance_classification_not_masqueraded():
    """F5：真实 verify 产出形状（acceptance 失败复用 _runtime_failure_state →
    rt=False+acc=False+classification）——拒因必须如实说"应用已启动、断言未过"，
    不得谎称"应用启动/探活失败"（应用明明起来了）。"""
    state = {**_GATE_BASE, "runtime_smoke_passed": False, "acceptance_passed": False,
             "verification_failure": "runtime_smoke",
             "runtime_smoke_details": {"classification": "acceptance_failed"}}
    allow, reason = can_auto_accept_delivery(state)
    assert allow is False
    assert reason.startswith("acceptance_failed"), "专类归因，不得冒充启动失败"
    assert "应用已启动" in reason
    assert "应用启动/探活失败" not in reason


def test_gate_runtime_false_migration_classification_typed():
    state = {**_GATE_BASE, "runtime_smoke_passed": False,
             "runtime_smoke_details": {"classification": "migration_failed"}}
    allow, reason = can_auto_accept_delivery(state)
    assert allow is False
    assert reason.startswith("migration_failed")
    assert "应用启动/探活失败" not in reason


def test_gate_runtime_false_default_message_unchanged_regression():
    # 无 details / 真启动失败分类 → 原启动失败文案不变
    for details in (None, {"classification": "code_error"}, {}):
        state = {**_GATE_BASE, "runtime_smoke_passed": False,
                 **({"runtime_smoke_details": details} if details is not None else {})}
        allow, reason = can_auto_accept_delivery(state)
        assert allow is False
        assert reason.startswith("runtime_smoke_failed"), details


def test_gate_ordering_runtime_before_acceptance_before_vf():
    # 判序：runtime 判之后、verification_failure 兜底之前（任务定案）
    allow, reason = can_auto_accept_delivery(
        {**_GATE_BASE, "runtime_smoke_passed": False, "acceptance_passed": False})
    assert allow is False and reason.startswith("runtime_smoke_failed")
    allow, reason = can_auto_accept_delivery(
        {**_GATE_BASE, "acceptance_passed": False, "verification_failure": "runtime_smoke"})
    assert allow is False and reason.startswith("acceptance_failed"), \
        "acceptance 专类先于 verification_failure 兜底"
    # l2 判序仍在最前（不破坏既有语义）
    allow, reason = can_auto_accept_delivery(
        {"l2_passed": False, "acceptance_passed": False})
    assert allow is False and reason.startswith("l2_failed")


# ═════════════ 4. deliver 人工闸 payload ═════════════

def _full_deliver_state():
    return {
        "task_id": "t-1",
        "task_description": "demo",
        "merged_diff": "diff --git a/x b/x",
        "l2_passed": True,
        "runtime_smoke_passed": True,
        "runtime_smoke_skipped": False,
        "runtime_smoke_message": "started ok",
        "runtime_smoke_details": {"classification": "passed_probe"},
        "migration_verify_passed": True,
        "migration_verify_details": {"kind": "flyway", "reason": "startup_ok"},
        "acceptance_passed": False,
        "acceptance_details": {
            "total": 3, "manual_count": 1, "failed_count": 1,
            "reason": "assertion_failed",
            "assertions": [
                {"id": "a-1", "req_id": "req-1", "verdict": "fail",
                 "request": {"method": "GET", "path": "/api/users"},
                 "http_code": 404, "reason": "期待 200 实得 404",
                 "body_excerpt": "X" * 500},
                {"id": "a-2", "req_id": "req-1", "verdict": "pass",
                 "request": {"method": "GET", "path": "/health"},
                 "http_code": 200, "reason": ""},
                {"id": "a-3", "req_id": "req-2", "kind": "http_probe",
                 "auth": "manual", "verdict": "skipped_manual"},
            ],
        },
        "requirement_items": [
            {"id": "req-1", "text": "用户列表接口"},
            {"id": "req-2", "text": "登录后可见的管理页"},
        ],
        "plan": _plan(_st("st-1", create=["a.py"], covers=["req-1"])),
        "degraded_reasons": ["acceptance_skipped:partial"],
    }


def test_deliver_payload_full_keys():
    from swarm.brain.nodes import _deliver_review_payload

    payload = _deliver_review_payload(_full_deliver_state())
    rt = payload["runtime_smoke"]
    assert rt["passed"] is True and rt["skipped"] is False
    assert rt["message"] == "started ok"
    assert rt["classification"] == "passed_probe"

    mig = payload["migration_verify"]
    assert mig["passed"] is True and mig["kind"] == "flyway"

    acc = payload["acceptance"]
    assert acc["passed"] is False
    assert acc["reason"] == "assertion_failed"
    assert acc["total"] == 3 and acc["manual_count"] == 1
    verdicts = {r["id"]: r["verdict"] for r in acc["assertions"]}
    assert verdicts == {"a-1": "fail", "a-2": "pass", "a-3": "skipped_manual"}
    row = next(r for r in acc["assertions"] if r["id"] == "a-1")
    assert row["method"] == "GET" and row["path"] == "/api/users"
    assert row["http_code"] == 404
    assert [m["id"] for m in acc["manual"]] == ["a-3"], "manual 清单单列供人工核验"

    cov = payload["coverage"]
    assert cov["total"] == 2 and cov["covered"] == 1
    assert [u["id"] for u in cov["uncovered"]] == ["req-2"]

    assert payload["degraded_reasons"] == ["acceptance_skipped:partial"]


def test_deliver_payload_old_checkpoint_missing_keys_tolerant():
    from swarm.brain.nodes import _deliver_review_payload

    payload = _deliver_review_payload({})  # 旧 checkpoint：全部新键缺失，不炸
    assert payload["runtime_smoke"]["passed"] is None
    assert payload["migration_verify"]["passed"] is None
    assert payload["acceptance"]["passed"] is None
    assert payload["acceptance"]["assertions"] == []
    assert payload["coverage"]["total"] == 0
    assert payload["coverage"]["uncovered"] == []
    assert payload["degraded_reasons"] == []


def test_deliver_payload_verdict_rows_capped_and_truncated():
    from swarm.brain.nodes import _deliver_review_payload

    state = _full_deliver_state()
    rows = [
        {"id": f"a-{i}", "req_id": "req-1", "verdict": "fail",
         "request": {"method": "GET", "path": f"/api/x/{i}"},
         "http_code": 500, "reason": "R" * 400}
        for i in range(25)
    ]
    state["acceptance_details"] = {"total": 25, "manual_count": 0,
                                   "failed_count": 25, "reason": "assertion_failed",
                                   "assertions": rows}
    payload = _deliver_review_payload(state)
    acc = payload["acceptance"]
    assert len(acc["assertions"]) == 20, "逐条 verdict 限量 20 条"
    assert acc["assertions_total"] == 25 and acc["assertions_omitted"] == 5
    assert all(len(r["reason"]) <= 160 for r in acc["assertions"]), "证据截断"


def test_deliver_interrupt_carries_review_payload(monkeypatch):
    # 行为集成：deliver 非 auto_accept 路径的 interrupt payload 必须带新键且旧键不丢
    import swarm.brain.nodes as nodes_pkg
    from swarm.types import HumanDecision

    captured: dict = {}

    def _fake_interrupt(payload):
        captured.update(payload)
        return {"decision": "accept"}

    monkeypatch.delenv("SWARM_AUTO_ACCEPT", raising=False)
    monkeypatch.setattr(nodes_pkg, "interrupt", _fake_interrupt)
    out = nodes_pkg.deliver({**_full_deliver_state(), "auto_accept": False})
    assert out["human_decision"] == HumanDecision.ACCEPT
    # 旧键（消费面兼容）
    assert captured["type"] == "deliver"
    assert captured["l2_passed"] is True
    assert captured["merged_diff"].startswith("diff --git")
    # 新键（S2-1 取证：人工审核此前连 runtime 结论都看不到）
    for key in ("runtime_smoke", "migration_verify", "acceptance",
                "coverage", "degraded_reasons"):
        assert key in captured, f"deliver payload 缺 {key}"
    assert captured["acceptance"]["passed"] is False
    assert captured["coverage"]["total"] == 2


# ═════════════ 5. LEARN 双闸（不改生产代码，锁定现状） ═════════════

_LEARN_BASE = {"l2_passed": True, "complexity": Complexity.MEDIUM}


def test_learn_acceptance_failed_round_never_learned_as_success():
    from swarm.memory.pattern_extractor import should_write_success
    assert should_write_success({**_LEARN_BASE, "acceptance_passed": False}) is False


def test_learn_acceptance_skipped_degraded_round_blocked_by_degraded_gate():
    from swarm.memory.pattern_extractor import should_write_success
    state = {**_LEARN_BASE, "acceptance_passed": None,
             "degraded_reasons": ["acceptance_skipped:all_manual"]}
    assert should_write_success(state) is False


def test_learn_positive_control_acceptance_passed_clean_writes():
    from swarm.memory.pattern_extractor import should_write_success
    assert should_write_success({**_LEARN_BASE, "acceptance_passed": True}) is True


# ═════════════ 5b. F2：信息性 degraded 白名单不掐死 L6 ═════════════

def test_learn_informational_degraded_does_not_block_l6():
    """F2：rejected=N 计数留痕/no_requirement_items 跳过是校验器【设计行为】的信息性
    留痕（常态每轮都有），不代表验证面降级——不得阻断 L6 成功学习。"""
    from swarm.memory.pattern_extractor import should_write_success
    state = {**_LEARN_BASE, "acceptance_passed": True, "degraded_reasons": [
        "requirements_extract:rejected=2(quote_not_in_sourcex2)",
        "acceptance_generation:rejected=1",
        "plan_coverage:skipped(no_requirement_items)",
    ]}
    assert should_write_success(state) is True


def test_learn_verification_face_degraded_still_blocks_l6():
    """F2 边界：验证面降级维持阻断（阶段1"跳过轮不写入成功记忆"承诺不放松）——
    即便混着信息性留痕。"""
    from swarm.memory.pattern_extractor import should_write_success
    for blocking in ("acceptance_skipped:all_manual",
                     "runtime_smoke_skipped:sandbox_unavailable",
                     "migration_verify_skipped:smoke_not_executed",
                     "requirements_extract:empty(all_rejected_or_empty)",
                     "plan_coverage:skipped(disabled)"):
        state = {**_LEARN_BASE, "degraded_reasons": [
            "acceptance_generation:rejected=1", blocking]}
        assert should_write_success(state) is False, blocking


def test_learn_empty_degraded_still_writes_regression():
    from swarm.memory.pattern_extractor import should_write_success
    assert should_write_success({**_LEARN_BASE, "degraded_reasons": []}) is True


# ═════════════ 6. F3：REVISE 清冻结断言 / replan 保留复用 ═════════════

def test_revise_clears_frozen_acceptance_assertions(monkeypatch):
    """F3：deliver REVISE=用户预期已变——revision 节点必须清空断言三键，让下一轮
    verify_runtime 按修订后 design/diff 重新生成（幂等复用只对"本轮已生成"成立）；
    requirement_items 不动（需求源文本未变）。"""
    import swarm.brain.nodes as nodes_pkg

    def _no_llm():
        raise RuntimeError("no llm in test")

    monkeypatch.setattr(nodes_pkg, "_get_brain_llm", _no_llm)
    monkeypatch.setattr(nodes_pkg, "_get_project_path", lambda pid: None)
    state = {
        "revision_feedback": "接口行为不符预期，请修订",
        "merged_diff": "", "task_description": "t", "project_id": "",
        "plan": _plan(_st("st-1", create=["a.py"])),
        "subtask_results": {"st-1": _wo("st-1")},
        "acceptance_assertions": [{"id": "a1", "kind": "http_probe"}],
        "acceptance_passed": True,
        "acceptance_details": {"reason": "all_passed"},
        "requirement_items": [{"id": "req-1", "text": "x"}],
    }
    out = asyncio.run(nodes_pkg.revision(state))
    assert out["acceptance_assertions"] == []
    assert out["acceptance_passed"] is None
    assert out["acceptance_details"] == {}
    assert "requirement_items" not in out, "需求条目不随 REVISE 清空（源文本未变）"


def test_replan_path_keeps_assertions_for_reuse():
    """F3 反面：handle_failure replan/定向恢复是代码级重做，不改需求——断言键不清，
    verify_runtime 幂等复用省 LLM。"""
    out = _run(_runtime_state(_acceptance_details()))
    for key in ("acceptance_assertions", "acceptance_passed", "acceptance_details"):
        assert key not in out, f"replan 路径不得清写 {key}（复用语义）"
