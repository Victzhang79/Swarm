#!/usr/bin/env python3
"""主题B 批二（round38c）—— B3 replan 计划级缺陷通道 + B4 scope 毒护栏/worker 异议。

取证（forensics_B3B4_code.md）：
  缺陷3：LLM replan 判决 10+ 次全被覆盖是结构必然（缺依赖定向恢复无条件抢跑+守卫见
    成功兄弟即降级）；"补一个 create_files"全决策面无合法通道；结构化载荷被 schema 丢弃。
  缺陷4：_derive_missing_type_files 全叉积把框架类名（SqlSessionTemplate）种进项目包
    路径锁死穷举；worker 无 scope 异议出口（notes 散文全仓零读点）。
治本：b3-3 schema 载荷真消费 / b3-1 计划缺口让位闸 / b3-2 外科补 create_files（限1次
  +防毒校验）/ b4-1 错误上下文共现配对替代全叉积 / b4-2 SCOPE_OBJECTION 行协议+消费。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.nodes as nodes  # noqa: E402
from swarm.brain.nodes.failure import _derive_missing_type_files  # noqa: E402
from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


def _st(sid, writable=None, create=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


class _FakeResp:
    def __init__(self, content):
        self.content = content


def _fake_llm(payload: str):
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp(payload)
    return lambda: _L()


# ── b4-1：证据配对——有 import 证据的框架类绝不与 blocked 内部包叉积 ──
def test_b41_evidence_pairing_excludes_framework_classes():
    build_output = (
        "src/main/java/com/x/Svc.java:3: error: package com.x.missing does not exist\n"
        "import com.x.missing.RealVO;\n"
        "                    ^\n"
        "src/main/java/com/x/Dao.java:2: error: package org.mybatis.spring does not exist\n"
        "import org.mybatis.spring.SqlSessionTemplate;\n"
        "src/main/java/com/x/Dao.java:9: error: cannot find symbol\n"
        "  symbol:   class SqlSessionTemplate\n"
        "  location: class com.x.Dao\n"
    )
    out = _derive_missing_type_files(
        ["mod/src/main/java/com/x/Svc.java"], ["com.x.missing"], build_output)
    assert "mod/src/main/java/com/x/missing/RealVO.java" in out, "真缺类必须推出"
    assert not any("SqlSessionTemplate" in p for p in out), (
        "import 证据指向非 blocked 包（框架/三方）的类绝不种进项目包路径——"
        "round38c st-30 被 SqlSessionTemplate.java 锁死 8 轮穷举的毒源")


# ── b4-1 回退语义保留：无任何 import 证据的自造引用照旧配对 blocked 包（round36 self-heal）──
def test_b41_evidence_free_class_falls_back_to_blocked_pkgs():
    build_output = ("[ERROR] cannot find symbol\n"
                    "  symbol:   class TwoFactorSetupVO\n"
                    "  location: var user\n")
    out = _derive_missing_type_files(
        ["mod/src/main/java/com/x/Svc.java"], ["com.ruoyi.system.domain.vo"], build_output)
    assert "mod/src/main/java/com/ruoyi/system/domain/vo/TwoFactorSetupVO.java" in out, (
        "无 import 证据的缺失类（worker 自造引用形态）保留旧回退——round36 self-heal "
        "依赖此语义（误配残余由 B4-2 异议通道兜底）")


# ── b3：计划缺口让位 + 外科补 create_files ──

_REPLAN_WITH_MISSING = (
    '{"strategy":"replan","reasoning":"BindVO 从未被任何子任务创建，需要 replan 加入 '
    'create_files","missing_files":["mod/src/main/java/com/x/vo/BindVO.java"]}'
)


def _gap_state(amend_counts=None):
    plan = TaskPlan(subtasks=[
        _st("st-ok", create=["mod/src/main/java/com/x/svc/OkService.java"]),
        _st("st-fail", writable=["mod/src/main/java/com/x/web/UseVO.java"]),
    ], parallel_groups=[["st-ok", "st-fail"]])
    ok = WorkerOutput(subtask_id="st-ok", diff="x", summary="", l1_passed=True,
                      confidence=Confidence.HIGH)
    fail = WorkerOutput(
        subtask_id="st-fail", diff="", summary="编译失败", l1_passed=False,
        l1_details={"build_output": "error: cannot find symbol class BindVO",
                    "failure_class": "capability"})
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-fail"],
        "subtask_results": {"st-ok": ok, "st-fail": fail},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    if amend_counts:
        state["subtask_scope_amend_counts"] = amend_counts
    return state


def test_b31_b32_plan_gap_bypasses_pom_recovery_and_amends_scope():
    state = _gap_state()
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_REPLAN_WITH_MISSING)):
        out = asyncio.run(nodes.handle_failure(state))
    # b3-1：不走缺依赖定向恢复（旧路径无条件抢跑 retry_alternate+补 pom）
    assert "targeted_recovery_counts" not in out, (
        "计划缺口（缺失文件全 plan 无 owner）必须让位——补 pom/换模型治不了计划缺 create_files")
    # b3-2：外科补 create_files 落地
    assert out.get("failure_strategy") == "retry"
    plan = out.get("plan")
    assert plan is not None, "外科修正必须显式 emit plan（in-place 变异靠捎带是被禁模式）"
    st_fail = {s.id: s for s in plan.subtasks}["st-fail"]
    assert "mod/src/main/java/com/x/vo/BindVO.java" in (st_fail.scope.create_files or []), (
        "LLM 点名且无 owner 的缺失文件必须补进失败子任务 create_files——"
        "这一最小计划修正动作此前全决策面无合法通道（TwoFactorBindVO 拖 3-5h）")
    assert out.get("subtask_scope_amend_counts", {}).get("st-fail") == 1
    assert "st-ok" in out.get("subtask_results", {}), "成功兄弟保留（守卫哲学不变）"


def test_b32_amend_bounded_once_per_subtask():
    state = _gap_state(amend_counts={"st-fail": 1})
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_REPLAN_WITH_MISSING)):
        out = asyncio.run(nodes.handle_failure(state))
    plan_subtasks = {s.id: s for s in state["plan"].subtasks}
    assert "mod/src/main/java/com/x/vo/BindVO.java" not in (
        plan_subtasks["st-fail"].scope.create_files or []), (
        "每子任务外科修正限 1 次——防 LLM 每轮点新文件导致 scope 无限膨胀震荡")
    assert out.get("failure_strategy") in ("retry", "retry_alternate"), "配额耗尽落原守卫降级"


# ── b4-2：worker scope 异议消费 ──

def _objection_state(suggested):
    plan = TaskPlan(subtasks=[
        _st("st-30", create=["mod/src/main/java/com/x/appsecret/SqlSessionTemplate.java"]),
    ], parallel_groups=[["st-30"]])
    fail = WorkerOutput(
        subtask_id="st-30", diff="", summary="这可能是一个错误", l1_passed=False,
        l1_details={"failure_class": "capability"},
        scope_objection={
            "file": "mod/src/main/java/com/x/appsecret/SqlSessionTemplate.java",
            "reason": "撞 MyBatis 框架类名",
            "suggested": suggested,
        })
    return {
        "plan": plan,
        "failed_subtask_ids": ["st-30"],
        "subtask_results": {"st-30": fail},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }


def test_b42_objection_replaces_poisoned_create_file():
    state = _objection_state("mod/src/main/java/com/x/appsecret/AppSecretValidateService.java")
    with patch.object(nodes, "_get_brain_llm", _fake_llm('{"strategy":"retry","reasoning":"r"}')):
        out = asyncio.run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "retry"
    plan = out.get("plan")
    assert plan is not None
    cf = {s.id: s for s in plan.subtasks}["st-30"].scope.create_files or []
    assert "mod/src/main/java/com/x/appsecret/AppSecretValidateService.java" in cf
    assert not any("SqlSessionTemplate" in f for f in cf), (
        "异议命中即替换毒条目——不再原样锁死让 worker 在'类名=文件名'里穷举 8 轮")
    assert out.get("subtask_scope_amend_counts", {}).get("st-30") == 1


def test_b42_objection_poison_suggested_rejected():
    state = _objection_state("../../../etc/evil.java")
    with patch.object(nodes, "_get_brain_llm", _fake_llm('{"strategy":"retry","reasoning":"r"}')):
        out = asyncio.run(nodes.handle_failure(state))
    cf = {s.id: s for s in state["plan"].subtasks}["st-30"].scope.create_files or []
    assert any("SqlSessionTemplate" in f for f in cf), "未过防毒校验的 suggested 不得应用"
    assert "subtask_scope_amend_counts" not in out or not out["subtask_scope_amend_counts"].get("st-30")


# ── b4-2 行协议解析 ──
def test_b42_parse_scope_objection_line():
    from swarm.worker.executor_l1gate import _L1GateMixin
    ns = SimpleNamespace(
        subtask=SimpleNamespace(id="st-1"),
        _get_git_diff=lambda: "",
        execution_log=[],
        _log=lambda *_a, **_k: None,
    )
    text = ('SUMMARY: 完成\nCONFIDENCE: low\nNOTES: 无\n'
            'SCOPE_OBJECTION: {"file": "a/B.java", "reason": "撞框架类", '
            '"suggested": "a/C.java"}')
    out = _L1GateMixin._parse_produce_result(ns, text, False, {})
    assert out.scope_objection == {"file": "a/B.java", "reason": "撞框架类",
                                   "suggested": "a/C.java"}
    # 坏 JSON 不阻断解析
    out2 = _L1GateMixin._parse_produce_result(ns, "SUMMARY: x\nSCOPE_OBJECTION: {bad", False, {})
    assert out2.scope_objection is None and out2.summary == "x"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("B批二 全部通过")
