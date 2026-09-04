#!/usr/bin/env python3
"""R65D-T5 plan 注入端：录制 plan 直入权威 VALIDATE 的离线调试通道。

round65d 教训：执行期 bug（H1 覆写/HANDLE_FAILURE 掉账/毒树合入）只能靠 live E2E 复现，
每次都要重烧云端规划期（~10min + $）。本通道把 cassette_extract 抽出的录制 plan 喂给
【新任务】，跳过整个云端规划子图（analyze→…→confirm），经确定性收尾器重跑出治后形态
后直接从 DISPATCH 进入 worker 阶段（执行期本就全本地）。

不变量（本文件锁死）：
① prepare_injected_state 对录制 cassette 做 fail-closed 校验：schema/空 plan/
   base_commit 不一致（含单侧缺失）一律 PlanInjectError，绝不带错基线开跑；
② 治后形态=重跑 finish_plan_deterministic（含 #61 reconcile_template_exam +
   #57 消费边）+ resolve_plan_conflicts，不是原样回放录制时的旧 plan；
③ 图入口=aupdate_state(as_node="confirm") 后 next 必须恰为 dispatch，
   路由不符 fail-loud（防 LangGraph 语义漂移把注入任务静默送错节点）；
④ SWARM_BRAIN_OFFLINE=1 时 brain 云端 LLM【构造点】即拦截（BrainOfflineError），
   调用方走各自既有降级路径——闸在构造点而非 20+ 调用点；
⑤ 脚手架剥离逻辑单一事实源在 brain/plan_inject.py，scripts/cassette_replay.py 引用。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_inject import (  # noqa: E402
    PlanInjectError,
    PlanInjectSeed,
    apply_plan_inject_seed,
    build_injected_initial_state,
    prepare_injected_state,
    strip_injected_scaffolds,
)
from swarm.brain.requirements_extract import TRUNCATION_MARKER, requirement_id  # noqa: E402
from swarm.brain.state import HumanDecision  # noqa: E402
from swarm.types import FileScope, SubTask, TaskPlan  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "plan_b583.json"


def _load_cassette() -> dict:
    cassette = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    cassette = {
        **cassette,
        # 本组测试关注 plan 治疗/接线，不复用旧 fixture 中未带完整分母的长 PRD。
        "task_description": "实现智能告警调度平台",
        "requirement_items": [{
            "text": "实现智能告警调度平台",
            "kind": "functional",
            "source_quote": "实现智能告警调度平台",
        }],
        "requirement_denominator_complete": True,
        "requirement_denominator_reason": "",
        "baseline_covered": [{
            "id": "req-existing",
            "reason": "现有服务已实现",
            "evidence": "ExistingService",
        }],
        "baseline_ineligible_reqs": ["req-rejected"],
    }
    cassette["task_description_len"] = len(cassette.get("task_description") or "")
    return cassette


# ── ① fail-closed 校验 ──

def test_prepare_rejects_schema_mismatch():
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state({"schema": "not-a-cassette", "plan": {"subtasks": []}},
                               live_base_commit=None, project_path=None)
    assert ei.value.code == "plan_inject_schema_invalid"


def test_prepare_rejects_empty_plan():
    c = _load_cassette()
    c = {**c, "plan": {"subtasks": []}}
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(c, live_base_commit=c["base_commit"], project_path=None)
    assert ei.value.code == "plan_inject_empty_plan"


def test_prepare_rejects_base_commit_mismatch():
    """★铁律：录制基线≠当前项目基线 → 绝不开跑（diff/merge/verify 全链会错基线错乱）★"""
    c = _load_cassette()
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(c, live_base_commit="deadbeef" * 5, project_path=None)
    assert ei.value.code == "plan_inject_base_commit_mismatch"
    # 错误信息必须告诉运维怎么修（重置基线到录制 commit）
    assert c["base_commit"][:12] in str(ei.value)


def test_prepare_rejects_one_sided_base_commit():
    """cassette 有基线而 live 捕获不到（非 git/仓库损坏）同样 fail-closed——
    单侧缺失≠等价，静默放行会让 worker diff base 漂到任意 HEAD。"""
    c = _load_cassette()
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(c, live_base_commit=None, project_path=None)
    assert ei.value.code == "plan_inject_base_commit_mismatch"


# ── ② 治后形态重推导 ──

def test_prepare_happy_path_rederives_treated_shape(caplog):
    c = _load_cassette()
    with caplog.at_level(logging.INFO):
        values = prepare_injected_state(
            c, live_base_commit=c["base_commit"], project_path=None,
            task_description=c.get("task_description", ""))
    plan = values["plan"]
    assert isinstance(plan, TaskPlan)
    # 功能子任务一个不丢
    orig_ids = {s["id"] for s in c["plan"]["subtasks"]}
    new_ids = {st.id for st in plan.subtasks}
    assert orig_ids <= new_ids, f"丢失功能子任务: {sorted(orig_ids - new_ids)[:5]}"
    # 治后证据（#61 考卷同源）：录制 plan 里 st-26 的 verify 还带着 round65d 死因本体
    # ——「grep jackson」内容断言与 okhttp 系权威模板自相矛盾（四面矛盾死局的一面）。
    # 重推导必须把它按模板重生成；仍留 jackson grep = 注入通道退化成旧 plan 原样回放。
    #（注：边总数不可作治后判据——fixture 抽自终态 checkpoint，420 边已含 C9 动态边，
    # 且 #61 反向边剪除会合法【减】边，实测 420→407。）
    orig_st26 = next(s for s in c["plan"]["subtasks"] if s["id"] == "st-26")
    orig_vcs = (orig_st26.get("harness") or {}).get("verify_commands") or []
    assert any("jackson" in v for v in orig_vcs), "fixture 前提变了：st-26 原考卷应含 jackson 断言"
    new_st26 = next(st for st in plan.subtasks if st.id == "st-26")
    new_vcs = (getattr(new_st26.harness, "verify_commands", []) or []) if new_st26.harness else []
    assert new_vcs and not any("jackson" in v for v in new_vcs), (
        f"st-26 考卷未被同源重生成（治后形态缺失）: {new_vcs[:4]}")
    # 图入口所需通道全备
    assert values["human_decision"] == HumanDecision.ACCEPT
    assert values["tech_design_file_plan"] == c["file_plan"]
    assert values["shared_contract"] == c["shared_contract"]
    assert "plan_valid" not in values, "局部 prepare 不得冒充完整 validate_plan 权威"
    assert values["requirement_denominator_complete"] is True
    assert len(values["requirement_items"]) == 1
    assert values["baseline_covered"][0]["id"] == "req-existing"
    assert values["baseline_ineligible_reqs"] == ["req-rejected"]
    # 机读账
    assert any("plan_inject_prepared" in r.message for r in caplog.records), \
        "注入准备必须落一行机读账（scaffolds/边数/剥离数）"


def test_prepare_rejects_unknown_requirement_denominator():
    """跳过抽取节点的注入任务必须自带已验证需求分母，旧 cassette 不得假装完整。"""
    c = _load_cassette()
    c.pop("requirement_denominator_complete")
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(
            c,
            live_base_commit=c["base_commit"],
            project_path=None,
            task_description=c["task_description"],
        )
    assert ei.value.code == "plan_inject_requirement_denominator_unknown"


@pytest.mark.parametrize("mode", ["reason", "marker", "item"])
def test_prepare_rejects_inconsistent_or_truncated_requirement_denominator(mode):
    c = _load_cassette()
    if mode == "marker":
        c["task_description"] += "\n" + TRUNCATION_MARKER
        c["task_description_len"] = len(c["task_description"])
    elif mode == "reason":
        c["requirement_denominator_reason"] = "source_truncated"
    else:
        c["requirement_items"][0]["source_truncated"] = True

    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(
            c,
            live_base_commit=c["base_commit"],
            project_path=None,
            task_description=c["task_description"],
        )

    assert ei.value.code == "plan_inject_requirement_denominator_unknown"


def test_prepare_rejects_live_description_different_from_recording():
    """录制计划只能服务原始需求，API 新描述不得借旧 cassette 的来源证据过闸。"""
    c = _load_cassette()
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(
            c,
            live_base_commit=c["base_commit"],
            project_path=None,
            task_description="另一项完全不同的任务",
        )
    assert ei.value.code == "plan_inject_description_mismatch"


def test_prepare_rejects_explicit_empty_live_description():
    c = _load_cassette()
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(
            c,
            live_base_commit=c["base_commit"],
            project_path=None,
            task_description="",
        )
    assert ei.value.code == "plan_inject_description_mismatch"


def test_prepare_rejects_tampered_description_length_provenance():
    c = _load_cassette()
    c["task_description_len"] += 1
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(
            c,
            live_base_commit=c["base_commit"],
            project_path=None,
            task_description=c["task_description"],
        )
    assert ei.value.code == "plan_inject_description_provenance_invalid"


def test_prepare_recomputes_requirement_floor_instead_of_trusting_old_flag():
    c = _load_cassette()
    c["task_description"] = "系统必须支持导入，并且必须报告错误，而且必须记录审计日志。"
    c["task_description_len"] = len(c["task_description"])
    c["requirement_items"] = [{
        "text": "支持导入",
        "kind": "functional",
        "source_quote": "系统必须支持导入",
    }]
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(
            c,
            live_base_commit=c["base_commit"],
            project_path=None,
            task_description=c["task_description"],
        )
    assert ei.value.code == "plan_inject_requirement_denominator_incomplete"


def test_injected_plan_can_reach_done_only_with_seeded_validation_provenance(monkeypatch):
    """官方注入路径须经权威验证取得正向证据后，才可能满足终态闸。"""
    from swarm.brain.gates import delivery_outcome
    from swarm.brain import nodes as brain_nodes

    requirement_text = "生成审计日志"
    req_id = requirement_id(requirement_text)
    c = {
        "schema": "swarm-plan-cassette/v1",
        "base_commit": None,
        "task_description": "系统必须生成审计日志。",
        "task_description_len": len("系统必须生成审计日志。"),
        "plan": {
            "subtasks": [{
                "id": "st-1",
                "description": "实现审计日志",
                "scope": {"create_files": ["a.py"]},
                "covers": [req_id],
            }],
        },
        "shared_contract": {},
        "file_plan": [{"module": "root", "path": "a.py", "action": "create"}],
        "requirement_items": [{
            "text": requirement_text,
            "kind": "functional",
            "source_quote": "系统必须生成审计日志",
        }],
        "requirement_denominator_complete": True,
        "requirement_denominator_reason": "",
    }
    values = prepare_injected_state(
        c,
        live_base_commit=None,
        project_path=None,
        task_description=c["task_description"],
    )

    class _ValidatingLLM:
        async def ainvoke(self, _messages):
            return type("Response", (), {"content": '{"valid": true, "issues": []}'})()

    monkeypatch.setattr(brain_nodes, "_get_project_path", lambda _pid: None)
    monkeypatch.setattr(brain_nodes, "_get_brain_llm", lambda: _ValidatingLLM())
    state = {
        **values,
        "complexity": "medium",
        "auto_accept": True,
        "l2_passed": True,
        "runtime_smoke_passed": True,
        "l3_passed": True,
        "acceptance_passed": True,
        "merged_diff": "diff --git a/a.py b/a.py",
    }

    validation = asyncio.run(brain_nodes.validate_plan(state))
    assert validation["plan_valid"] is True
    assert delivery_outcome({**state, **validation}) == "DONE"


def test_injected_plan_does_not_bypass_authoritative_coverage_gate():
    """prepare 的局部结构校验通过，也必须把缺 covers 的 plan 交给权威 coverage 闸打回。"""
    from swarm.brain import nodes as brain_nodes

    c = _load_cassette()
    c = {
        **c,
        "plan": {
            "subtasks": [{
                "id": "st-1",
                "description": "只实现文件，不声明覆盖需求",
                "scope": {"create_files": ["a.py"]},
                "covers": [],
            }],
        },
        "shared_contract": {},
        "file_plan": [{"module": "root", "path": "a.py", "action": "create"}],
    }
    values = prepare_injected_state(
        c, live_base_commit=c["base_commit"], project_path=None,
        task_description=c["task_description"],
    )

    validation = asyncio.run(brain_nodes.validate_plan({**values, "complexity": "medium"}))

    assert validation["plan_valid"] is False
    assert validation["plan_validation_gate"] == "coverage"


def test_injected_plan_preserves_baseline_coverage_for_authoritative_validation(monkeypatch):
    """不在 TaskPlan 内的存量覆盖账也必须穿过 extractor/prepare 到达 coverage 闸。"""
    from swarm.brain import nodes as brain_nodes
    import swarm.brain.baseline_candidates as baseline_candidates

    requirement_text = "沿用现有审计日志"
    req_id = requirement_id(requirement_text)
    c = {
        "schema": "swarm-plan-cassette/v1",
        "base_commit": None,
        "task_description": "系统必须沿用现有审计日志。",
        "task_description_len": len("系统必须沿用现有审计日志。"),
        "plan": {
            "subtasks": [{
                "id": "st-1",
                "description": "调整文档",
                "scope": {"create_files": ["note.txt"]},
                "covers": [],
            }],
        },
        "shared_contract": {},
        "file_plan": [{"module": "root", "path": "note.txt", "action": "create"}],
        "requirement_items": [{
            "text": requirement_text,
            "kind": "functional",
            "source_quote": "系统必须沿用现有审计日志",
        }],
        "requirement_denominator_complete": True,
        "baseline_covered": [{
            "id": req_id,
            "reason": "ExistingAuditService 已提供该能力",
            "evidence": "ExistingAuditService",
        }],
        "baseline_ineligible_reqs": [],
    }
    values = prepare_injected_state(
        c, live_base_commit=None, project_path=None,
        task_description=c["task_description"],
    )

    class _ValidatingLLM:
        async def ainvoke(self, _messages):
            return type("Response", (), {"content": '{"valid": true, "issues": []}'})()

    async def _vocab(_state):
        return {"existingauditservice"}

    async def _file_index(_state):
        return {"service.py": {"existingauditservice"}}

    monkeypatch.setattr(brain_nodes, "_get_project_path", lambda _pid: None)
    monkeypatch.setattr(brain_nodes, "_get_brain_llm", lambda: _ValidatingLLM())
    monkeypatch.setattr(brain_nodes, "_baseline_vocab_for", _vocab)
    monkeypatch.setattr(brain_nodes, "_baseline_file_index_for", _file_index)
    monkeypatch.setattr(baseline_candidates, "baseline_claims_missing_evidence", lambda *_a: [])
    monkeypatch.setattr(baseline_candidates, "baseline_claims_unground", lambda *_a: [])

    validation = asyncio.run(brain_nodes.validate_plan({**values, "complexity": "medium"}))

    assert values["baseline_covered"][0]["id"] == req_id
    assert validation["plan_valid"] is True


def test_error_message_carries_machine_code():
    """猎手 HIGH 锁：PlanInjectError 可能从任何出口冒泡（闸3 在 _stream_brain_events
    深处触发时走 generic FAILED 归一 error=str(exc)[:300]）——message 必须自带 code。"""
    e = PlanInjectError("plan_inject_route_mismatch", "详情")
    assert str(e).startswith("plan_inject_route_mismatch: ")


def test_prepare_fails_closed_when_treatment_pass_swallowed(monkeypatch):
    """★猎手 HIGH 锁★：finisher/inject 包装的治疗 pass 全 fail-open（live 由 VALIDATE
    兜底，注入通道没有）——考卷同源 reconcile 被吞异常时，毒考卷 plan 绝不能带着
    plan_inject_prepared 成功账开跑（round65d 冻结陈旧模板死型）。"""
    import swarm.brain.contract_utils as cu
    c = _load_cassette()

    def _boom(plan):
        raise RuntimeError("reconcile 内部炸了")

    monkeypatch.setattr(cu, "reconcile_template_exam", _boom)
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(c, live_base_commit=c["base_commit"], project_path=None)
    assert ei.value.code == "plan_inject_rederive_degraded"


def test_prepare_wraps_resolve_conflicts_failure(monkeypatch):
    """猎手 MEDIUM 锁：resolve_plan_conflicts 内部无 fail-open——意外异常必须归一为
    机读拒绝码，绝不裸冒泡成无码 FAILED。"""
    import swarm.brain.contract_utils as cu
    c = _load_cassette()

    def _boom(plan, project_path=None, base_ref=None):
        raise RuntimeError("normalize 崩了")

    monkeypatch.setattr(cu, "resolve_plan_conflicts", _boom)
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(c, live_base_commit=c["base_commit"], project_path=None)
    assert ei.value.code == "plan_inject_rederive_failed"


def test_reject_path_writes_full_terminal_accounting(monkeypatch):
    """复核 HIGH 锁：注入拒绝=FAILED 终态，必须与 runner 其余终态写点同口径三件套
    （token_usage 机读账 salvage_reason / 站内通知 / audit 留痕）。"""
    from swarm.brain import runner as r
    calls: dict[str, object] = {}

    monkeypatch.setattr(r.store, "update_task",
                        lambda tid, **kw: calls.setdefault("update", kw))
    monkeypatch.setattr(r.store, "get_task", lambda tid: {"id": tid, "project_id": "p"})
    monkeypatch.setattr(r, "_emit_task_notification",
                        lambda tid, rec, st: calls.setdefault("notify", st))
    monkeypatch.setattr(r, "audit",
                        lambda event, **kw: calls.setdefault("audit", (event, kw)))

    async def _fake_emit(topic, event):
        calls.setdefault("emitted", event)

    monkeypatch.setattr(r, "_emit", _fake_emit)
    exc = PlanInjectError("plan_inject_base_commit_mismatch", "基线漂了")
    asyncio.run(r._reject_plan_inject("t-x", "p-x", exc, object()))

    upd = calls["update"]
    assert upd["status"] == "FAILED"
    assert upd["error"].startswith("plan_inject_base_commit_mismatch")
    assert upd["token_usage"]["salvage_reason"] == "plan_inject_base_commit_mismatch"
    assert calls["notify"] == "FAILED"
    assert calls["audit"][0] == "task_failed"
    assert calls["emitted"]["status"] == "failed", "必须向 SSE 通道发失败事件"


def test_surgical_topup_llm_failure_yields_to_full_replan(monkeypatch, caplog):
    """猎手 INFO 锁：外科补齐取 brain LLM 失败（含 SWARM_BRAIN_OFFLINE 闸）必须保守
    让路全量重拆（return None），绝不裸抛经 plan() 把整任务打成 FAILED。"""
    import swarm.brain.nodes as nodes
    import swarm.brain.plan_validator as pv
    monkeypatch.setenv("SWARM_MODULE_COHERENCE_GATE", "0")
    monkeypatch.setattr(pv, "build_coverage_matrix",
                        lambda *a, **k: {"uncovered": ["r1"], "dangling_covers": [],
                                         "items": [{"id": "r1"}]})

    def _boom():
        raise RuntimeError("offline gate")

    monkeypatch.setattr(nodes, "_get_brain_llm", _boom)
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d", scope=FileScope(writable=["a.py"]))])
    state = {"plan": plan, "plan_validation_feedback": "覆盖缺口: r1",
             "replan_feedback": "", "project_id": "p-x", "complexity": "medium",
             "requirement_items": [{"id": "r1", "text": "需求r1"}]}
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(nodes._maybe_surgical_coverage_topup(state))
    assert out is None
    assert any("外科补齐取 brain LLM 失败" in rec.message for rec in caplog.records), \
        "必须走到让路护栏（而非某个前置早退）——护栏未被执行到则本测试为假锁"


def test_prepare_structurally_invalid_plan_fails_closed():
    """注入预检先拒绝环状 DAG，避免把明显坏快照写入图 checkpoint。"""
    c = _load_cassette()
    cyc = {
        "subtasks": [
            {"id": "st-a", "description": "a", "depends_on": ["st-b"],
             "scope": {"writable": ["a.py"]}},
            {"id": "st-b", "description": "b", "depends_on": ["st-a"],
             "scope": {"writable": ["b.py"]}},
        ],
    }
    c = {**c, "plan": cyc}
    with pytest.raises(PlanInjectError) as ei:
        prepare_injected_state(c, live_base_commit=c["base_commit"], project_path=None)
    assert ei.value.code == "plan_inject_validation_failed"


def test_prepare_greenfield_both_none_passes():
    """双侧都无基线（greenfield/非 git）= 无基线可错，放行但告警。"""
    c = _load_cassette()
    c = {**c, "base_commit": None}
    values = prepare_injected_state(c, live_base_commit=None, project_path=None)
    assert isinstance(values["plan"], TaskPlan)


# ── ③ 图入口：恰入 dispatch，路由不符 fail-loud ──

def _mini_values(decision) -> dict:
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d", scope=FileScope(writable=["a.py"]))])
    return {"task_id": "t-inject-ut", "project_id": "p-x", "plan": plan,
            "human_decision": decision, "task_description": "d"}


def test_seed_enters_graph_exactly_at_authoritative_validation(monkeypatch):
    from swarm.brain.graph import compile_brain_graph
    monkeypatch.setattr("swarm.tracing.configure_langsmith", lambda: False)
    graph = compile_brain_graph(None)
    config = {"configurable": {"thread_id": "t-inject-route-ok"}}
    asyncio.run(apply_plan_inject_seed(graph, config, _mini_values(HumanDecision.ACCEPT)))
    snap = asyncio.run(graph.aget_state(config))
    assert tuple(snap.next) == ("validate_plan",), f"注入后 next={snap.next}"


def test_seed_route_mismatch_fails_loud():
    class _WrongGraph:
        async def aupdate_state(self, *_args, **_kwargs):
            return None

        async def aget_state(self, _config):
            return type("Snapshot", (), {"next": ("dispatch",)})()

    graph = _WrongGraph()
    config = {"configurable": {"thread_id": "t-inject-route-bad"}}
    with pytest.raises(PlanInjectError) as ei:
        asyncio.run(apply_plan_inject_seed(
            graph, config, _mini_values(HumanDecision.ACCEPT)))
    assert ei.value.code == "plan_inject_route_mismatch"


# ── ④ SWARM_BRAIN_OFFLINE 构造点闸 ──

def test_brain_offline_gate_blocks_llm_construction(monkeypatch):
    monkeypatch.setenv("SWARM_BRAIN_OFFLINE", "1")
    from swarm.models.router import BrainOfflineError, ModelRouter
    # 构造器会读取能力库/密钥存储；本测试只锁 getter 最前端闸，绕开外部 PG。
    r = ModelRouter.__new__(ModelRouter)
    with pytest.raises(BrainOfflineError):
        r.get_brain_llm()
    with pytest.raises(BrainOfflineError):
        r.get_brain_fallback_llm()


def test_brain_offline_gate_default_off(monkeypatch):
    """默认（env 未设）绝不拦截——正常任务的 brain 调用零影响。"""
    monkeypatch.delenv("SWARM_BRAIN_OFFLINE", raising=False)
    from swarm.models.router import BrainOfflineError, ModelRouter
    r = ModelRouter.__new__(ModelRouter)
    try:
        r.get_brain_llm()
    except BrainOfflineError:  # pragma: no cover
        pytest.fail("未开闸却被 BrainOfflineError 拦截")
    except Exception:
        pass  # 测试环境无云端配置时的其它构造失败与本闸无关


def test_brain_offline_fallback_getter_degrades_to_none(monkeypatch):
    """nodes._get_brain_fallback_llm 文档口径=取用失败返回 None（调用方降级）——
    offline 闸必须落进这条既有降级路径而不是炸穿。"""
    monkeypatch.setenv("SWARM_BRAIN_OFFLINE", "1")
    from swarm.brain.nodes import _get_brain_fallback_llm
    assert _get_brain_fallback_llm() is None


# ── ⑤ 剥离逻辑单一事实源 ──

def test_strip_scaffolds_single_source_in_replay_script():
    _rp = Path(__file__).resolve().parent.parent / "scripts" / "cassette_replay.py"
    _rp_spec = importlib.util.spec_from_file_location("cassette_replay_t5", _rp)
    m = importlib.util.module_from_spec(_rp_spec)
    sys.modules["cassette_replay_t5"] = m
    _rp_spec.loader.exec_module(m)
    assert m._strip_injected_scaffolds is strip_injected_scaffolds, \
        "cassette_replay 必须引用 brain.plan_inject 的单一事实源，不许本地复刻"


def test_strip_scaffolds_behavior():
    plan = TaskPlan(subtasks=[
        SubTask(id="st-scaffold-mod", description="pom",
                scope=FileScope(writable=["mod/pom.xml"])),
        SubTask(id="st-1", description="d", depends_on=["st-scaffold-mod"],
                scope=FileScope(writable=["a.py"]))])
    n = strip_injected_scaffolds(plan)
    assert n == 1
    assert [st.id for st in plan.subtasks] == ["st-1"]
    assert plan.subtasks[0].depends_on == []


def test_submitter_preserves_recorded_description_whitespace(monkeypatch, tmp_path):
    """官方提交器不得 strip 原文，否则 runner 的 cassette 来源绑定会误杀合法快照。"""
    submit_path = Path(__file__).resolve().parent.parent / "scripts" / "plan_inject_submit.py"
    submit_spec = importlib.util.spec_from_file_location("plan_inject_submit_test", submit_path)
    submit_module = importlib.util.module_from_spec(submit_spec)
    submit_spec.loader.exec_module(submit_module)
    raw_description = "系统必须生成审计日志。\n"
    cassette_path = tmp_path / "cassette.json"
    cassette_path.write_text(json.dumps({
        "schema": "swarm-plan-cassette/v1",
        "task_id": "old",
        "base_commit": None,
        "task_description": raw_description,
        "plan": {"subtasks": [{"id": "st-1"}]},
    }, ensure_ascii=False), encoding="utf-8")
    captured: dict = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"status":"submitted","task":{"id":"new"}}'

    def _urlopen(request, timeout):
        captured.update(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr(submit_module.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(sys, "argv", [
        "plan_inject_submit.py", str(cassette_path), "--project", "p", "--token", "x",
    ])

    assert submit_module.main() == 0
    assert captured["description"] == raw_description


# ── 存储/入口面接线 ──

def test_task_record_carries_injected_plan_column():
    from swarm.project import store
    assert "injected_plan" in store._TASK_SELECT, "任务详情必须能读回注入 plan"
    import inspect
    assert "injected_plan" in inspect.signature(store.create_task).parameters


def test_cassette_extractor_carries_requirement_provenance(monkeypatch, tmp_path):
    """生产抽取入口必须把同一 checkpoint 的需求条目与完整性成对写进 cassette。"""
    from swarm.brain import graph, runner

    extract_path = Path(__file__).resolve().parent.parent / "scripts" / "cassette_extract.py"
    extract_spec = importlib.util.spec_from_file_location("cassette_extract_batch3", extract_path)
    extract_module = importlib.util.module_from_spec(extract_spec)
    extract_spec.loader.exec_module(extract_module)

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d", scope=FileScope(writable=["a.py"]))
    ])

    async def _pg_ready():
        return True

    async def _snapshot(_task_id, thread_id=None):
        return {
            "plan": plan,
            "project_id": "p1",
            "project_path": str(tmp_path),
            "task_description": "系统必须生成审计日志。",
            "clarify_summary": "日志必须包含请求标识。",
            "requirement_items": [{
                "id": "req-a",
                "text": "生成审计日志",
                "source_quote": "系统必须生成审计日志",
                "kind": "functional",
            }],
            "requirement_denominator_complete": True,
            "requirement_denominator_reason": "",
            "baseline_covered": [{"id": "req-a", "reason": "已有实现"}],
            "baseline_ineligible_reqs": ["req-old-fake"],
        }

    monkeypatch.setattr(graph, "init_postgres_checkpointer", _pg_ready)
    monkeypatch.setattr(runner, "_load_state_snapshot", _snapshot)

    cassette = asyncio.run(extract_module._extract("t1", None))

    assert cassette["requirement_items"][0]["text"] == "生成审计日志"
    assert cassette["requirement_denominator_complete"] is True
    assert cassette["requirement_denominator_reason"] == ""
    assert cassette["clarify_summary"] == "日志必须包含请求标识。"
    assert cassette["baseline_covered"] == [{"id": "req-a", "reason": "已有实现"}]
    assert cassette["baseline_ineligible_reqs"] == ["req-old-fake"]


def test_api_gate_default_closed():
    """注入端默认关死（SWARM_PLAN_INJECT_ENABLE 未设 → 拒绝），内部调试通道绝不裸奔。"""
    from swarm.api.routers.task import TaskCreateRequest, _plan_inject_enabled
    assert _plan_inject_enabled() is False
    assert "injected_plan" in TaskCreateRequest.model_fields


def test_runner_seed_wrapper_type_exists():
    """runner 分支判据=PlanInjectSeed 包装类型（与 Command resume 判据同法）。"""
    seed = PlanInjectSeed(values={"task_id": "x"})
    assert seed.values["task_id"] == "x"


def test_injected_cycle_discards_retry_worker_results_and_verification_state():
    old = {
        "task_id": "t", "plan": "old-plan",
        "subtask_results": {"st-1": {"diff": "+OLD", "l1_passed": True}},
        "coverage_watermark": ["old-req"], "l2_passed": True,
        "dispatch_remaining": ["old-st"],
    }
    prepared = {"plan": "new-plan", "requirement_items": [{"id": "new-req"}]}

    clean = build_injected_initial_state(old, prepared)

    assert clean["task_id"] == "t" and clean["plan"] == "new-plan"
    assert "subtask_results" not in clean
    assert "coverage_watermark" not in clean
    assert "dispatch_remaining" not in clean
    assert "l2_passed" not in clean


def test_prepare_always_emits_adjudication_ledger():
    """H-6（批次8 hunter R1 CONFIRMED 整改②）：注入=新规划周期从空账重推导，finisher/
    resolve 的新裁决必须入账并 always-emit 落键——否则回退 PLAN 的 reconcile 与 attach
    前置核对这批违例失明（账与 file_plan 漂移，复活面重开）。always-emit 防 checkpoint
    回退丢键（round67h 教训）。"""
    c = _load_cassette()
    values = prepare_injected_state(
        c, live_base_commit=c["base_commit"], project_path=None,
        task_description=c.get("task_description", ""))
    assert "file_plan_adjudications" in values, "注入通道必须 always-emit 裁决账键"
    assert isinstance(values["file_plan_adjudications"], list)
    for adj in values["file_plan_adjudications"]:
        assert {"pass", "action", "path"} <= set(adj), f"裁决账条目缺机读字段: {adj}"
