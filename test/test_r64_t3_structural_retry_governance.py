#!/usr/bin/env python3
"""R64-T3：结构性校验失败的重试治理——外科补齐让路 + 同签名收敛熔断。

round64 实锤（swarm.log 3822 行逐行通读 + cassette 58 次调用亲核）：
- pass1/pass3 走 P1 外科补齐（只补覆盖），对 G1 结构性违例注定空转，白耗 2 次重试额度；
- pass2 全量重产 33min，输出 sql 处置与初版【逐字节相同】（G1 反馈注入 plan_batch，但该
  节点 schema 无 module/file_plan 字段 + P4 禁改前缀，反馈结构性无法执行）；
- 三次违例签名完全一致却无熔断 → 68min/~50% plan_batch token 纯浪费。

治本（确定性止损，不新增易粘滞 state 键）：
① `_maybe_surgical_coverage_topup` 增加模块 coherence 前置核——上一版 plan 结构性
  违例时让路全量重拆（外科只补覆盖救不了结构）。
② G1 失败时记录违例签名（绑定 retry 轮次，防跨 replan 周期陈旧误熔断）；连续两轮签名
  一致 → 把 plan_retry_count 顶到 MAX_PLAN_RETRY，复用既有 after_validate 路由直接
  CONFIRM fail-fast，绝不再烧一轮 33min 全量重产。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
)


def _st(sid, create_files, covers=None):
    sc = FileScope(writable=[], readable=[], create_files=create_files)
    st = SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                 modality=SubTaskModality.TEXT, scope=sc)
    if covers is not None:
        st.covers = covers
    return st


def _incoherent_plan():
    """G1 违①：模块 alarm-api 双源码根（真结构性违例，非 R64-T1 的辅助文件误伤）。"""
    return TaskPlan(
        subtasks=[_st("a", ["alarm-api/src/main/java/A.java",
                            "ruoyi-alarm/alarm-api/src/main/java/B.java"])],
        parallel_groups=[["a"]])


_INCOHERENT_FP = [
    {"module": "alarm-api", "path": "alarm-api/src/main/java/A.java"},
    {"module": "alarm-api", "path": "ruoyi-alarm/alarm-api/src/main/java/B.java"},
]


# ── ① 外科补齐对结构性违例让路 ──

@pytest.mark.asyncio
async def test_surgical_topup_declines_on_incoherent_prior_plan(monkeypatch):
    """上一版 plan 模块 coherence 违例 → 外科补齐必须让路（返回 None 走全量重拆），
    绝不调 LLM 做注定空转的定向补覆盖（round64 pass1/pass3 白耗 2 次重试额度）。"""
    import swarm.brain.nodes as nodes_pkg
    _n = nodes_pkg

    called = {"topup": False}

    async def _fake_topup(*a, **k):
        called["topup"] = True
        return (_incoherent_plan(), None)

    monkeypatch.setattr(_n, "_targeted_coverage_topup", _fake_topup)
    monkeypatch.setattr(_n, "_get_project_path", lambda pid: None)
    state = {
        "complexity": "ultra",
        "plan_validation_feedback": "模块 'alarm-api' 在计划里对应【多个物理目录】…",
        "replan_feedback": "",
        "plan": _incoherent_plan(),
        "requirement_items": [{"id": "REQ-1", "text": "需求一"}],
        "tech_design_file_plan": _INCOHERENT_FP,
        "project_id": "p-t3",
    }
    out = await _n._maybe_surgical_coverage_topup(state)
    assert out is None, "结构性违例的上一版 plan 绝不许走外科补齐"
    assert not called["topup"], "让路必须发生在 LLM 调用之前（不烧定向补齐）"


@pytest.mark.asyncio
async def test_surgical_topup_still_works_for_pure_coverage(monkeypatch):
    """coherent 的 plan 纯覆盖缺口 → 外科路径保持可用（治本不砍 P1 的合法面）。"""
    import swarm.brain.nodes as _n

    sentinel_plan = TaskPlan(
        subtasks=[_st("a", ["m1/src/main/java/A.java"], covers=["REQ-1"])],
        parallel_groups=[["a"]])

    async def _fake_topup(*a, **k):
        return (sentinel_plan, None)

    monkeypatch.setattr(_n, "_targeted_coverage_topup", _fake_topup)
    monkeypatch.setattr(_n, "_get_project_path", lambda pid: None)
    monkeypatch.setattr(_n, "_format_project_structure", lambda kc: "")
    state = {
        "complexity": "ultra",
        "plan_validation_feedback": "覆盖缺口：REQ-2 未覆盖",
        "replan_feedback": "",
        "plan": sentinel_plan,
        "requirement_items": [{"id": "REQ-1", "text": "一"}, {"id": "REQ-2", "text": "二"}],
        "tech_design_file_plan": [
            {"module": "m1", "path": "m1/src/main/java/A.java"}],
        "project_id": "p-t3",
    }
    out = await _n._maybe_surgical_coverage_topup(state)
    assert out is not None and out[0] is sentinel_plan, \
        "coherent plan 的纯覆盖重试必须保留外科路径（零回归）"


# ── ② G1 同签名收敛熔断 ──

def _g1_state(retry, prev=None):
    st = {
        "plan": _incoherent_plan(),
        "task_description": "t",
        "complexity": "ultra",
        "plan_retry_count": retry,
        "tech_design_file_plan": _INCOHERENT_FP,
        "project_id": "p-t3",
        "requirement_items": [],
    }
    if prev is not None:
        st["plan_validation_prev_structural"] = prev
    return st


def _run_validate(state, monkeypatch):
    import swarm.brain.nodes as _n
    monkeypatch.setattr(_n, "_get_project_path", lambda pid: None)
    return asyncio.run(_n.validate_plan(state))


def test_g1_failure_records_signature(monkeypatch):
    """G1 首败：打回 + 记录违例签名（绑定 retry 轮次），retry 计数不被顶格。"""
    out = _run_validate(_g1_state(retry=0), monkeypatch)
    assert out["plan_valid"] is False
    assert out["plan_retry_count"] == 0, "首败绝不熔断"
    prev = out.get("plan_validation_prev_structural")
    assert prev and prev.get("retry") == 0 and prev.get("sig"), \
        f"G1 失败必须记录签名供下轮比对: {prev}"


def test_g1_same_signature_two_rounds_fuses(monkeypatch, caplog):
    """★熔断本体★ 连续两轮同签名（上轮 retry=N-1）→ plan_retry_count 顶到
    MAX_PLAN_RETRY，after_validate 直接 CONFIRM——绝不再烧 33min 全量重产。"""
    import logging

    from swarm.brain.graph import MAX_PLAN_RETRY, after_validate

    first = _run_validate(_g1_state(retry=0), monkeypatch)
    prev = first["plan_validation_prev_structural"]
    with caplog.at_level(logging.WARNING):
        second = _run_validate(_g1_state(retry=1, prev=prev), monkeypatch)
    assert second["plan_valid"] is False
    assert second["plan_retry_count"] >= MAX_PLAN_RETRY, \
        "同签名两轮必须熔断（顶格 retry 计数复用既有路由）"
    assert any("熔断" in r.message for r in caplog.records), "熔断必须可观测"
    # 路由终判：确定性 CONFIRM（auto_accept fail-fast / 人工闸），不再回 PLAN
    route = after_validate({**_g1_state(retry=second["plan_retry_count"]),
                            "plan_valid": False})
    assert route == "confirm"


def test_g1_stale_signature_from_old_cycle_does_not_fuse(monkeypatch):
    """陈旧签名防误熔断：replan 周期重置 retry 后，旧周期残留签名（retry 不连续）
    绝不触发熔断——新周期必须至少获得一次带反馈的重试机会。"""
    stale = {"sig": ["x"], "retry": 2}
    out = _run_validate(_g1_state(retry=0, prev=stale), monkeypatch)
    assert out["plan_retry_count"] == 0, "retry 不连续的陈旧签名绝不熔断"


def test_g1_different_signature_does_not_fuse(monkeypatch):
    """签名变了=LLM 真在修（哪怕还没修对）→ 不熔断，给足 MAX_PLAN_RETRY。"""
    prev = {"sig": ["完全不同的违例"], "retry": 0}
    out = _run_validate(_g1_state(retry=1, prev=prev), monkeypatch)
    assert out["plan_valid"] is False
    assert out["plan_retry_count"] == 1, "签名变化时保持常规重试路径"


# ── 对抗复核整改锁 ──

@pytest.mark.asyncio
async def test_surgical_precheck_respects_kill_switch(monkeypatch):
    """★复核 LOW 锁★ SWARM_MODULE_COHERENCE_GATE=0（G1 泄压阀）时外科前置核同步失效
    ——否则闸关了这里还强制全量重拆，杀开关名存实亡。"""
    import swarm.brain.nodes as _n

    sentinel = _incoherent_plan()

    async def _fake_topup(*a, **k):
        return (sentinel, None)

    monkeypatch.setenv("SWARM_MODULE_COHERENCE_GATE", "0")
    monkeypatch.setattr(_n, "_targeted_coverage_topup", _fake_topup)
    monkeypatch.setattr(_n, "_get_project_path", lambda pid: None)
    monkeypatch.setattr(_n, "_format_project_structure", lambda kc: "")
    state = {
        "complexity": "ultra",
        "plan_validation_feedback": "覆盖缺口",
        "replan_feedback": "",
        "plan": sentinel,
        "requirement_items": [{"id": "REQ-1", "text": "一"}],
        "tech_design_file_plan": _INCOHERENT_FP,
        "project_id": "p-t3",
    }
    out = await _n._maybe_surgical_coverage_topup(state)
    assert out is not None, "泄压阀开启时外科路径必须保持可用（与 G1 闸同律）"


def test_confirm_revise_resets_retry_and_signature(monkeypatch):
    """★复核 MEDIUM 锁★ 人工 REVISE=新规划周期：必须重置 plan_retry_count 与结构违例
    签名——否则熔断顶格(retry=3)后 REVISE 的新 plan 一旦校验失败即被 retry>=MAX 直送
    confirm，人工反馈得不到任何自动重试（软锁死）。"""
    import swarm.brain.nodes as _n

    # 自我隔离：confirm_plan 读 SWARM_AUTO_ACCEPT env——全量套件里其它测试的残留值会让
    # 本测试走 auto_accept 分支根本到不了 interrupt（全量实测被污染挂过一次）。
    monkeypatch.delenv("SWARM_AUTO_ACCEPT", raising=False)
    monkeypatch.setattr(_n, "interrupt",
                        lambda payload: {"decision": "revise", "feedback": "改一下模块拆分"})
    out = _n.confirm_plan({
        "plan": _incoherent_plan(),
        "plan_retry_count": 3,
        "plan_validation_prev_structural": {"sig": ["x"], "retry": 1},
        "auto_accept": False,
        "complexity": "ultra",
    })
    assert out.get("plan_retry_count") == 0, "REVISE 必须重置 retry（对称 failure.py 三出口）"
    assert out.get("plan_validation_prev_structural") == {}, "REVISE 必须清结构签名"


def test_fuse_kill_switch(monkeypatch):
    """★猎手 F4 锁★ SWARM_G1_RETRY_FUSE=0 → 熔断关闭，同签名照常规重试路径走。"""
    monkeypatch.setenv("SWARM_G1_RETRY_FUSE", "0")
    first = _run_validate(_g1_state(retry=0), monkeypatch)
    out = _run_validate(
        _g1_state(retry=1, prev=first["plan_validation_prev_structural"]), monkeypatch)
    assert out["plan_retry_count"] == 1, "泄压阀关闭时绝不顶格"


def test_validate_success_clears_signature(monkeypatch):
    """★猎手 F1 锁★ 校验通过必须清结构签名——残留签名会与下一 replan 周期的新失败
    发生相邻巧合（prev.retry==0 且新周期 retry=1 时 G1 首败）误熔断。"""
    import swarm.brain.nodes as _n
    coherent = TaskPlan(
        subtasks=[_st("a", ["m1/src/main/java/A.java"])], parallel_groups=[["a"]])
    monkeypatch.setattr(_n, "_get_project_path", lambda pid: None)
    # 关软校验/覆盖闸走纯确定性通过路径
    monkeypatch.setenv("SWARM_PLAN_COVERAGE_GATE", "0")
    monkeypatch.setenv("SWARM_PLAN_LLM_VALIDATION", "0")
    out = asyncio.run(_n.validate_plan({
        "plan": coherent, "task_description": "t", "complexity": "medium",
        "plan_retry_count": 0, "requirement_items": [],
        "plan_validation_prev_structural": {"sig": ["stale"], "retry": 0},
    }))
    if out.get("plan_valid"):
        assert out.get("plan_validation_prev_structural") == {}, \
            "通过路径必须清签名（防跨周期相邻巧合误熔断）"


def test_failure_replan_exits_clear_signature():
    """★猎手 F1 锁★ failure.py 三个 replan 出口重置 plan_retry_count 的同时必须清
    结构签名（与 plan_validation_feedback 同律对称）。"""
    src = Path(__file__).resolve().parents[1] / "brain" / "nodes" / "failure.py"
    text = src.read_text()
    n_retry_resets = text.count('"plan_retry_count": 0,')
    n_sig_clears = text.count('"plan_validation_prev_structural": {},')
    assert n_retry_resets >= 3
    assert n_sig_clears == n_retry_resets, \
        f"retry 重置点 {n_retry_resets} 与签名清理点 {n_sig_clears} 必须一一对称"


def test_state_schema_registers_prev_structural():
    """LangGraph 未声明键=静默丢弃（批4a 实证）——新键必须进 BrainState + 生命周期表。"""
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState

    assert "plan_validation_prev_structural" in BrainState.__annotations__
    assert ACCOUNTING_KEY_LIFECYCLE.get("plan_validation_prev_structural") == "round"
