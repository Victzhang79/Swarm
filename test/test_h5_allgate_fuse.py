"""R67J-H5：validate 全闸收敛熔断账（R64-T3 从 G1 泛化到全硬闸）。

体检 TOP5（跨闸弹跳耗尽共享 retry 预算）：validate 逐闸早退（structure→C1→C2→R40-1→G1→
覆盖闸共享 plan_retry_count=3），熔断账此前仅 G1 分支写——每轮死在不同闸时账链断裂、无收敛
判定，3 轮各修一闸即预算耗尽，表观死因=最后一闸、真死因="逐闸串行修"且可能在回退重犯。

治本（轻量版，用户拍板）：prev_structural 账升级为【全闸账】{gate, sig, retry, count,
gate_1ago, count_1ago}；两触发均加 gate 相等约束——
  触发一：同闸同签名连续两轮（反馈未被执行，重试必然同结果）；
  触发二：同闸 2 轮窗口违例数零净收敛（含跨闸弹跳回退：A闸→B闸→A闸 且 A 违例数未减）。
跨闸计数异质绝不互比（不同闸 issue 语料不可比，2→10→2 的真进展绝不误熔）；
"修好一闸进下一闸"的前进流不熔（gate 不等→两触发皆假）。全 G1 序列严格退化为 R64-T3 现行为。
排除面（fail-open 保重试）：plan 空/整模块分解失败（瞬态倾向，重试真可能救）、structure 闸
（issue 文本去 st-id 归一后判别力不足，同签名≠同违例，纳入会误熔）。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.graph import MAX_PLAN_RETRY  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
)


def _st(sid, create_files, depends=None):
    sc = FileScope(writable=[], readable=[], create_files=create_files)
    return SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                   modality=SubTaskModality.TEXT, scope=sc,
                   depends_on=depends or [])


def _r40_state(retry, orphans=("m1/src/main/java/Orphan.java",), prev=None):
    """构造只在 R40-1（file_plan 归属闸）失败的 state：orphans 无 owner，其余全过。"""
    plan = TaskPlan(
        subtasks=[_st("a", ["m1/src/main/java/A.java"]),
                  _st("b", ["m1/src/main/java/B.java"], depends=["a"])],
        parallel_groups=[["a"], ["b"]])
    st = {
        "plan": plan, "task_description": "t", "complexity": "ultra",
        "plan_retry_count": retry, "project_id": "p-h5", "requirement_items": [],
        "tech_design_file_plan": (
            [{"module": "m1", "path": "m1/src/main/java/A.java"},
             {"module": "m1", "path": "m1/src/main/java/B.java"}]
            + [{"module": "m1", "path": o} for o in orphans]),
    }
    if prev is not None:
        st["plan_validation_prev_structural"] = prev
    return st


def _g1_state(retry, prev=None):
    """构造只在 G1（模块 coherence）失败的 state：alarm-api 双物理根。"""
    plan = TaskPlan(
        subtasks=[_st("a", ["alarm-api/src/main/java/A.java",
                            "ruoyi-alarm/alarm-api/src/main/java/B.java"])],
        parallel_groups=[["a"]])
    st = {
        "plan": plan, "task_description": "t", "complexity": "ultra",
        "plan_retry_count": retry, "project_id": "p-h5", "requirement_items": [],
        "tech_design_file_plan": [
            {"module": "alarm-api", "path": "alarm-api/src/main/java/A.java"},
            {"module": "alarm-api", "path": "ruoyi-alarm/alarm-api/src/main/java/B.java"}],
    }
    if prev is not None:
        st["plan_validation_prev_structural"] = prev
    return st


def _run(state, monkeypatch):
    import swarm.brain.nodes as _n
    monkeypatch.setattr(_n, "_get_project_path", lambda pid: None)
    return asyncio.run(_n.validate_plan(state))


# ── 账面：非 G1 硬闸失败也必须写全闸账 ──────────────────────────────────────────

def test_r40_failure_writes_gate_account(monkeypatch):
    """R40-1 首败：打回 + 写全闸账（gate 标记闸名，绑定 retry 轮次），绝不熔断。"""
    out = _run(_r40_state(retry=0), monkeypatch)
    assert out["plan_valid"] is False
    assert out["plan_retry_count"] == 0, "首败绝不熔断"
    acct = out.get("plan_validation_prev_structural")
    assert acct, "非 G1 硬闸失败必须写收敛账（H-5 本体：账链跨闸不断裂）"
    assert acct.get("gate") == "R40-1"
    assert acct.get("retry") == 0 and acct.get("sig") and acct.get("count") == 1


# ── 触发一泛化：同闸同签名连续两轮 → 熔断 ──────────────────────────────────────

def test_r40_same_signature_two_rounds_fuses(monkeypatch, caplog):
    import logging
    first = _run(_r40_state(retry=0), monkeypatch)
    with caplog.at_level(logging.WARNING):
        second = _run(_r40_state(retry=1, prev=first["plan_validation_prev_structural"]),
                      monkeypatch)
    assert second["plan_valid"] is False
    assert second["plan_retry_count"] >= MAX_PLAN_RETRY, \
        "R40-1 同签名连续两轮（反馈未被执行）必须与 G1 同律熔断"
    assert any("熔断" in r.message for r in caplog.records)


# ── 触发二泛化：同闸 2 轮窗口 count 零净收敛（签名逐轮换）→ 熔断 ────────────────

def test_r40_window_same_gate_changing_sig_fuses(monkeypatch):
    """同族换件重犯：每轮孤儿文件不同（签名互不相交）但违例数不降 → 窗口熔断。"""
    r0 = _run(_r40_state(retry=0, orphans=("m1/src/main/java/O1.java",)), monkeypatch)
    r1 = _run(_r40_state(retry=1, orphans=("m1/src/main/java/O2.java",),
                         prev=r0["plan_validation_prev_structural"]), monkeypatch)
    assert r1["plan_retry_count"] == 1, "窗口未满（retry<2）绝不熔断"
    r2 = _run(_r40_state(retry=2, orphans=("m1/src/main/java/O3.java",),
                         prev=r1["plan_validation_prev_structural"]), monkeypatch)
    assert r2["plan_retry_count"] >= MAX_PLAN_RETRY, \
        "同闸 2 轮窗口违例数 1→1→1 零净收敛必须熔断（同族换件重犯）"


# ── 跨闸弹跳：回退重犯熔断 / 真进展绝不误熔 ────────────────────────────────────

def test_cross_gate_bounceback_equal_count_fuses(monkeypatch):
    """A闸→B闸→A闸 弹跳回退：R40-1 曾过闸（round1 死在 G1 说明 R40-1 已修好）又被
    重产打破且违例数未减 = 零净收敛 → 熔断（H-5 核心场景：跨闸弹跳耗尽预算）。"""
    r0 = _run(_r40_state(retry=0, orphans=("m1/src/main/java/O1.java",)), monkeypatch)
    assert r0["plan_validation_prev_structural"]["gate"] == "R40-1"
    r1 = _run(_g1_state(retry=1, prev=r0["plan_validation_prev_structural"]), monkeypatch)
    assert r1["plan_retry_count"] == 1, "换闸首见绝不熔断"
    assert r1["plan_validation_prev_structural"]["gate"] == "G1"
    r2 = _run(_r40_state(retry=2, orphans=("m1/src/main/java/O2.java",),
                         prev=r1["plan_validation_prev_structural"]), monkeypatch)
    assert r2["plan_retry_count"] >= MAX_PLAN_RETRY, \
        "弹跳回退（R40-1→G1→R40-1 且 R40-1 违例数未减）必须熔断"


def test_cross_gate_excursion_with_progress_no_fuse(monkeypatch):
    """弹跳但同闸违例数净下降（2→…→1）= LLM 真在修 → 绝不误熔，给足重试。"""
    r0 = _run(_r40_state(retry=0, orphans=("m1/src/main/java/O1.java",
                                           "m1/src/main/java/O2.java")), monkeypatch)
    assert r0["plan_validation_prev_structural"]["count"] == 2
    r1 = _run(_g1_state(retry=1, prev=r0["plan_validation_prev_structural"]), monkeypatch)
    r2 = _run(_r40_state(retry=2, orphans=("m1/src/main/java/O3.java",),
                         prev=r1["plan_validation_prev_structural"]), monkeypatch)
    assert r2["plan_valid"] is False
    assert r2["plan_retry_count"] == 2, "同闸违例数 2→1 净下降是真进展，绝不误熔"


def test_forward_gate_progression_no_fuse(monkeypatch):
    """修好一闸进下一闸（R40-1→G1，gate 不等）= 前进流 → 两触发皆不成立。"""
    r0 = _run(_r40_state(retry=0), monkeypatch)
    r1 = _run(_g1_state(retry=1, prev=r0["plan_validation_prev_structural"]), monkeypatch)
    assert r1["plan_retry_count"] == 1


# ── 兼容 / 排除面 / 泄压阀 ─────────────────────────────────────────────────────

def test_old_format_account_without_gate_treated_as_g1(monkeypatch):
    """在飞 checkpoint 升级兼容：旧账无 gate 键（历史唯一写者是 G1）→ 按 G1 论，
    G1 同签名连续两轮照常熔断（升级瞬间不失防护）。"""
    first = _run(_g1_state(retry=0), monkeypatch)
    legacy = {k: v for k, v in first["plan_validation_prev_structural"].items()
              if k in ("sig", "retry", "count", "count_1ago")}
    out = _run(_g1_state(retry=1, prev=legacy), monkeypatch)
    assert out["plan_retry_count"] >= MAX_PLAN_RETRY


def test_plan_none_does_not_write_account(monkeypatch):
    """排除面：plan 空（瞬态倾向，重试真可能救）不写账——账链自然断裂 fail-open 保重试。"""
    out = _run({"plan": None, "task_description": "t", "complexity": "ultra",
                "plan_retry_count": 1, "requirement_items": []}, monkeypatch)
    assert out["plan_valid"] is False
    assert "plan_validation_prev_structural" not in out


def test_kill_switch_covers_all_gates(monkeypatch):
    """泄压阀 SWARM_G1_RETRY_FUSE=0 覆盖全闸（单一开关，绝不留只关 G1 的半开状态）。"""
    monkeypatch.setenv("SWARM_G1_RETRY_FUSE", "0")
    first = _run(_r40_state(retry=0), monkeypatch)
    out = _run(_r40_state(retry=1, prev=first["plan_validation_prev_structural"]),
               monkeypatch)
    assert out["plan_retry_count"] == 1, "泄压阀关闭时绝不顶格"


def test_helper_itemized_count_resolution(monkeypatch):
    """★复核 M1 锁★ 逐条化 issues 的 count 语义：同闸窗口内条目数净下降（2→1）绝不熔
    ——防"合成汇总串 count 恒 1 → 任意两次单条目事件平凡熔断"退化（coverage_watermark
    防御分支今日不可达，此处直测 helper 锁语义）。"""
    import swarm.brain.nodes as _n
    r0_retry, r0 = _n._gate_fuse_and_account(
        {}, "coverage_watermark",
        ["coverage_watermark 倒退：req-aaaaaaaa 已达成覆盖丢失",
         "coverage_watermark 倒退：req-bbbbbbbb 已达成覆盖丢失"], 0)
    assert r0_retry == 0 and r0["count"] == 2
    r1_retry, r1 = _n._gate_fuse_and_account(
        {"plan_validation_prev_structural": r0}, "G1", ["模块违例 x"], 1)
    assert r1_retry == 1
    r2_retry, r2 = _n._gate_fuse_and_account(
        {"plan_validation_prev_structural": r1}, "coverage_watermark",
        ["coverage_watermark 倒退：req-cccccccc 已达成覆盖丢失"], 2)
    assert r2_retry == 2, "同闸条目数 2→1 净下降=真进展，绝不熔断"
    # 反向：条目数未减（1→…→1 不同 req）→ 熔断（零净收敛）
    b0_retry, b0 = _n._gate_fuse_and_account(
        {}, "coverage_watermark",
        ["coverage_watermark 倒退：req-aaaaaaaa 已达成覆盖丢失"], 0)
    b1_retry, b1 = _n._gate_fuse_and_account(
        {"plan_validation_prev_structural": b0}, "G1", ["模块违例 x"], 1)
    b2_retry, _ = _n._gate_fuse_and_account(
        {"plan_validation_prev_structural": b1}, "coverage_watermark",
        ["coverage_watermark 倒退：req-dddddddd 已达成覆盖丢失"], 2)
    assert b2_retry >= MAX_PLAN_RETRY, "同闸弹跳回退且条目数未减必须熔断"


def test_nonconsecutive_stale_account_no_fuse(monkeypatch):
    """retry 不连续（中间隔了不写账的轮/新周期）→ 陈旧账绝不熔断（R64-T3 原护栏跨闸保持）。"""
    stale = {"gate": "R40-1", "sig": ["x"], "retry": 3, "count": 1}
    out = _run(_r40_state(retry=1, prev=stale), monkeypatch)
    assert out["plan_retry_count"] == 1
