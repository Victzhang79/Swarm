#!/usr/bin/env python3
"""Q4 规划子图节点单测（批次①）— 纯逻辑/构造-state，不接 graph、不调真 LLM。

覆盖：
  clarify    — 微任务跳过 / 自动化跳过 / 轮数上限
  assess     — 新建项目最低 complex 升级 / 微任务直 simple
  review     — 自动化自动通过 / 打回达上限强制通过
  elaborate  — 超预算标记 oversized / 无验收 INVEST 计数
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import planning_nodes as P
from swarm.types import Complexity


# ── clarify ───────────────────────────────────────────────
def test_clarify_micro_task_skips():
    out = asyncio.run(P.clarify({"is_micro_task": True, "task_description": "按钮黄改绿"}))
    assert out["clarify_done"] is True
    print("  ✅ clarify: 微任务跳过澄清")


def test_clarify_auto_mode_skips():
    out = asyncio.run(P.clarify({"auto_accept": True, "task_description": "x"}))
    assert out["clarify_done"] is True
    print("  ✅ clarify: 自动化模式跳过澄清")


def test_clarify_round_cap():
    # 已达上限轮次 → 直接结束，不调 LLM
    out = asyncio.run(P.clarify({"clarify_round": P.MAX_CLARIFY_ROUNDS, "task_description": "x"}))
    assert out["clarify_done"] is True
    print(f"  ✅ clarify: 达 {P.MAX_CLARIFY_ROUNDS} 轮上限结束")


# ── assess ────────────────────────────────────────────────
def test_assess_micro_is_simple():
    out = asyncio.run(P.assess({"is_micro_task": True}))
    assert out["assessed_complexity"] == Complexity.SIMPLE
    print("  ✅ assess: 微任务直接 simple")


def test_assess_greenfield_min_complex():
    # 新建项目即使 LLM 判 simple，也升到 complex（需技术方案）。mock LLM 返回 simple。
    class _FakeResp:
        content = '{"complexity":"simple","reason":"x","needs_tech_design":false}'

    class _FakeLLM:
        async def ainvoke(self, _msgs):
            return _FakeResp()

    _orig = P._get_brain_llm
    P._get_brain_llm = lambda: _FakeLLM()
    try:
        out = asyncio.run(P.assess({
            "session_metadata": {"greenfield": True},
            "task_description": "写个推箱子",
            "clarify_summary": "前端 canvas，后端无",
        }))
        assert out["assessed_complexity"] in (Complexity.COMPLEX, Complexity.ULTRA)
        print("  ✅ assess: 新建项目最低升 complex")
    finally:
        P._get_brain_llm = _orig


# ── review_design ─────────────────────────────────────────
def test_review_auto_approves():
    out = asyncio.run(P.review_design({"auto_accept": True, "tech_design": {}}))
    assert out["design_review"]["decision"] == "approve"
    print("  ✅ review: 自动化模式自动通过")


def test_review_reject_cap_forces_approve(monkeypatch):
    # 钉住 env：非 auto 的强制通过路径不受宿主 .env（SWARM_AUTO_ACCEPT=1）污染
    monkeypatch.delenv("SWARM_AUTO_ACCEPT", raising=False)
    out = asyncio.run(P.review_design({
        "design_review": {"reject_count": P.MAX_DESIGN_REJECTS},
        "tech_design": {},
    }))
    assert out["design_review"]["decision"] == "approve"
    assert out["design_review"].get("forced") is True
    print(f"  ✅ review: 打回达 {P.MAX_DESIGN_REJECTS} 次上限强制通过")


def _review_with_decision(monkeypatch, decision_payload):
    """非 auto、未达上限 → 走 interrupt；patch interrupt 直接返回 resume payload。
    钉住 env：默认行为测试不受宿主 .env（e2e profile 的 SWARM_AUTO_ACCEPT=1）污染。"""
    monkeypatch.delenv("SWARM_AUTO_ACCEPT", raising=False)
    monkeypatch.setattr(P, "interrupt", lambda _payload: decision_payload)
    return asyncio.run(P.review_design({"auto_accept": False, "tech_design": {}}))


def test_review_explicit_approve(monkeypatch):
    out = _review_with_decision(monkeypatch, {"decision": "approve"})
    assert out["design_review"]["decision"] == "approve"
    print("  ✅ review: 显式 approve 通过")


def test_review_explicit_reject(monkeypatch):
    out = _review_with_decision(monkeypatch, {"decision": "reject", "feedback": "x"})
    assert out["design_review"]["decision"] == "reject"
    assert out["design_review"]["reject_count"] == 1
    print("  ✅ review: 显式 reject 打回")


def test_review_unknown_payload_failclosed_to_reject(monkeypatch):
    """fail-closed：畸形/未知 decision 不再静默 approve，按打回处理。"""
    for bad in ({"unexpected": "shape"}, "garbled", None, {"decision": "maybe"}):
        out = _review_with_decision(monkeypatch, bad)
        assert out["design_review"]["decision"] == "reject", f"未知 {bad!r} 应 fail-closed 打回"
    print("  ✅ review: 未知/畸形决策 fail-closed 打回(非静默 approve)")


# ── elaborate（用真实 SubTask/TaskPlan，避免 mock drift）──
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _real_sub(sid, est=0, acc=None, deps=None, readable=None, writable=None):
    return SubTask(
        id=sid, description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [], readable=readable or []),
        acceptance_criteria=acc or [], est_context_tokens=est,
        depends_on=deps or [],
    )


def test_elaborate_resplits_oversized():
    # 超预算子任务 → 二次拆分。mock LLM 拆成 2 个各自在预算内的子任务。
    budget = P._context_budget()

    class _R:
        content = ('{"subtasks":[{"description":"part A","acceptance_criteria":["a"],"est_context_tokens":40000},'
                   '{"description":"part B","acceptance_criteria":["b"],"est_context_tokens":40000}]}')

    class _L:
        async def ainvoke(self, m): return _R()

    _orig = P._get_brain_llm
    P._get_brain_llm = lambda: _L()
    try:
        plan = TaskPlan(subtasks=[_real_sub("st-1", est=budget + 50_000, acc=["x"])], parallel_groups=[["st-1"]])
        out = asyncio.run(P.elaborate({"plan": plan, "task_id": ""}))
        new_plan = out.get("plan")
        assert new_plan is not None, "二次拆分应回写 plan"
        assert len(new_plan.subtasks) == 2, f"超预算子任务应拆成 2 个，实际 {len(new_plan.subtasks)}"
        assert not out["oversized_subtask_ids"], "拆分后应不再超预算"
        print("  ✅ elaborate: 超预算子任务被二次拆分(1→2)，拆后不再超预算")
    finally:
        P._get_brain_llm = _orig


def test_elaborate_normal_no_resplit():
    # 预算内子任务不拆
    plan = TaskPlan(subtasks=[_real_sub("st-1", est=1000, acc=["x"])], parallel_groups=[["st-1"]])
    out = asyncio.run(P.elaborate({"plan": plan, "task_id": ""}))
    assert out.get("plan") is None, "未拆分不应回写 plan"
    assert not out["oversized_subtask_ids"]
    print("  ✅ elaborate: 预算内子任务不拆分")


def test_elaborate_invest_counts_missing_acceptance():
    plan = TaskPlan(subtasks=[_real_sub("st-1", est=1000, acc=None), _real_sub("st-2", est=1000, acc=["x"])],
                    parallel_groups=[["st-1", "st-2"]])
    out = asyncio.run(P.elaborate({"plan": plan, "task_id": ""}))
    assert out["invest_fail_count"] == 1
    print("  ✅ elaborate: 无验收标准计入 invest_fail")


# ── P0-1: 二次拆分后下游依赖重映射（复现 task 0f93f1fc 规划死循环）──
# 批5：_remap_dependents（死代码，生产用 _remap_dependents_to_terminals）随两条结构测试一并删除。


def test_needs_resplit_single_file_guard():
    """单文件修改(恰好1个writable、0新建)绝不二次拆分——拆同一文件会 diff 冲突坏 patch
    (task 8c9782b4 实证)。多文件/0文件(greenfield)仍按预算判。"""
    budget = P._context_budget()
    # 单文件超预算 → 守卫禁拆
    single = _real_sub("s1", est=budget + 99_999, writable=["StringUtils.java"])
    assert P._needs_resplit(single, budget) is False, "单文件超预算也不该拆"
    # 多文件超预算 → 应拆
    multi = _real_sub("s2", est=budget + 99_999, writable=["A.java", "B.java"])
    assert P._needs_resplit(multi, budget) is True, "多文件超预算应拆"
    # 0文件(greenfield)超预算 → 不受单文件守卫限制，按预算判应拆
    green = _real_sub("s3", est=budget + 99_999, writable=[])
    assert P._needs_resplit(green, budget) is True, "0文件超预算应可拆"
    print("  ✅ _needs_resplit: 单文件禁拆/多文件可拆/greenfield可拆")


def test_elaborate_resplit_no_dangling_dependency():
    """端到端复现 task 0f93f1fc：st-1 超预算被二次拆分，st-2 depends_on st-1。
    修复前 st-2 依赖悬空 → VALIDATE_PLAN 必报"依赖未知任务 st-1"陷入死循环。
    修复后 st-2 应依赖子链尾节点，全图无悬空依赖。"""
    budget = P._context_budget()

    class _R:
        content = ('{"subtasks":[{"description":"part A","acceptance_criteria":["a"],"est_context_tokens":40000},'
                   '{"description":"part B","acceptance_criteria":["b"],"est_context_tokens":40000}]}')

    class _L:
        async def ainvoke(self, m): return _R()

    _orig = P._get_brain_llm
    P._get_brain_llm = lambda: _L()
    try:
        # st-1 产出 NumberUtils.java（writable），st-2 读它（readable）形成真实耦合，
        # 避免被 _decouple_independent_subtasks 当假依赖剥离（还原 task 0f93f1fc 真实场景：
        # st-2 的 StringUtils.isNumericStr 真依赖 st-1 产出的 NumberUtils 契约）。
        # st-1 给【2 个 writable】触发二次拆分（单文件已被守卫禁拆——拆同一文件会 diff 冲突，
        # 见 _needs_resplit 单文件守卫）；多文件拆分才是合法场景。
        plan = TaskPlan(
            subtasks=[
                _real_sub("st-1", est=budget + 50_000, acc=["x"],
                          writable=["NumberUtils.java", "MathUtils.java"]),
                _real_sub("st-2", est=1000, acc=["y"], deps=["st-1"],
                          readable=["NumberUtils.java"], writable=["StringUtils.java"]),
            ],
            parallel_groups=[["st-1"], ["st-2"]],
        )
        out = asyncio.run(P.elaborate({"plan": plan, "task_id": ""}))
        new_plan = out.get("plan")
        assert new_plan is not None, "二次拆分应回写 plan"
        ids = {s.id for s in new_plan.subtasks}
        # st-1 被拆成 st-1-1/st-1-2，原 st-1 不再存在
        assert "st-1" not in ids, "拆分后 st-1 应被替换"
        assert {"st-1-1", "st-1-2", "st-2"} <= ids, f"应含拆分子节点+st-2，实际 {ids}"
        # 关键断言：全图无悬空依赖（所有 depends_on 目标都存在）
        for s in new_plan.subtasks:
            for d in (s.depends_on or []):
                assert d in ids, f"悬空依赖: {s.id} depends_on 不存在的 {d}（修复失败）"
        # st-2 应改依赖子链尾节点 st-1-2
        st2 = next(s for s in new_plan.subtasks if s.id == "st-2")
        assert "st-1-2" in st2.depends_on, f"st-2 应依赖尾节点 st-1-2，实际 {st2.depends_on}"
        print("  ✅ elaborate: 二次拆分后下游依赖重映射，全图无悬空依赖（task 0f93f1fc 死循环已修复）")
    finally:
        P._get_brain_llm = _orig


# ── 简易 LLM mock fixture（assess greenfield 用）──
def monkeypatch_llm():
    """非 pytest 运行时的占位；pytest 下由下方 conftest 风格 fixture 注入。"""
    pass


if __name__ == "__main__":
    print("=" * 56)
    print("  Q4 规划子图节点单测（批次①）")
    print("=" * 56)
    passed = failed = 0
    tests = [
        test_clarify_micro_task_skips, test_clarify_auto_mode_skips, test_clarify_round_cap,
        test_assess_micro_is_simple, test_assess_greenfield_min_complex,
        test_review_auto_approves, test_review_reject_cap_forces_approve,
        test_elaborate_resplits_oversized, test_elaborate_normal_no_resplit,
        test_elaborate_invest_counts_missing_acceptance,
        test_elaborate_resplit_no_dangling_dependency,
    ]
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1

    print("=" * 56)
    print(f"  📊 结果: {passed} 通过, {failed} 失败")
    print("=" * 56)
    import sys
    sys.exit(1 if failed else 0)
