"""阶段3.4（登记册 §二 A6+A9）：覆盖闸出口——缺口≤阈值 degraded 放行 + 反馈分页轮转。

A6：CONFIRM 全有全无（2/108 未覆盖=整任务 REJECT）→ 缺口≤阈值（max(GAP_MAX,
  total×RATIO)，默认 2/3%）且【纯缺口】（无悬空 covers/臆造 baseline/水位倒退）且
  已给过≥1 轮修补机会 → degraded 放行：plan_valid=True + degraded_reasons 留痕
  （残差进 deliver 覆盖矩阵可观测面 + 阻断 L6 假成功学习），feedback 清空。
  水位倒退（3.1 硬地板）绝不放行——缺口只许是"从未覆盖"，不许是"倒退出来的"。
A9：覆盖反馈 8000 字符截断 → 每轮暴露另一批震荡（round34 实证 18→12→18）。
  改分页轮转：超帽时按 retry_count 轮转页窗，页头自述（未列出≠已解决）。
  R67M2-T1 A1（24号文）：帽 8000→32000——打回池全是阻断级 issue，现实池（数十条）
  藏任何一条=重产对该死因失明（round67m2：17 条池分 2 页藏 3 条致命 ③b→熔断误
  顶格）；分页轮转仅剩病态池（数百条+）安全阀语义。
  外科补齐（P1 topup）从 ULTRA-only 放开到 MEDIUM/COMPLEX（仅 SIMPLE 除外）。
"""

from __future__ import annotations

import pytest

from swarm.brain.nodes import _format_validation_feedback, validate_plan
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

REQS = [f"req-{i:04d}aaaa"[:12] for i in range(10)]


def _items(n=10):
    return [{"id": REQS[i], "text": f"需求条目{i}的功能描述", "kind": "functional",
             "source_quote": f"条目{i}", "source": "description"} for i in range(n)]


def _st(sid, writable=None, covers=None, desc="do"):
    return SubTask(id=sid, description=desc, difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=list(writable or []), readable=[]),
                   covers=list(covers or []), depends_on=[])


def _plan_covering(ids):
    return TaskPlan(subtasks=[_st("st-1", writable=["a"], covers=list(ids))],
                    parallel_groups=[["st-1"]])


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    async def ainvoke(self, messages):
        return _Resp('{"valid": true, "issues": []}')


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("SWARM_PLAN_COVERAGE_GAP_MAX", "SWARM_PLAN_COVERAGE_GAP_RATIO",
              "SWARM_VALIDATE_PLAN_LLM_GATE", "SWARM_PLAN_COVERAGE_TOPUP"):
        monkeypatch.delenv(k, raising=False)
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    yield


async def _run(plan, retry=1, watermark=None, items=None):
    st = {"plan": plan, "task_description": "t", "complexity": "medium",
          "plan_retry_count": retry, "requirement_items": items or _items()}
    if watermark is not None:
        st["coverage_watermark"] = watermark
    return await validate_plan(st)


# ─────────────── A6：degraded 放行 ───────────────

async def test_gap_within_threshold_degraded_pass_after_retry():
    out = await _run(_plan_covering(REQS[:9]), retry=1)  # 1/10 缺口 ≤ 阈值(2)
    assert out["plan_valid"] is True, "缺口≤阈值且已给修补机会→degraded 放行（替代全有全无）"
    # 语义演进（阶段3.9 H-F5）：残差从 append-only degraded_reasons（无人能清→缺口
    # 补齐后仍永久拦 L6）迁到独立 last-write-wins 键。意图不变：拦 L6+deliver 可见。
    assert out.get("coverage_gap_residual") == [REQS[9]], (
        "残差必须留痕（阻断假成功学习+deliver 可观测），且 id 可见")
    assert out["plan_validation_feedback"] == ""
    assert sorted(out.get("coverage_watermark") or []) == sorted(REQS[:9])


async def test_gap_first_attempt_still_rejected():
    out = await _run(_plan_covering(REQS[:9]), retry=0)
    assert out["plan_valid"] is False, "首轮必须先给修补机会（P1 topup/D09），不许直接放弃"


async def test_gap_over_threshold_rejected():
    out = await _run(_plan_covering(REQS[:5]), retry=1)  # 5/10 缺口 > 阈值
    assert out["plan_valid"] is False


async def test_dangling_covers_never_degraded_pass():
    p = TaskPlan(subtasks=[_st("st-1", writable=["a"],
                               covers=REQS[:9] + ["req-ghost999"])],
                 parallel_groups=[["st-1"]])
    out = await _run(p, retry=1)
    assert out["plan_valid"] is False, "悬空 covers=臆造信号，绝不 degraded 放行"


async def test_watermark_regression_never_degraded_pass():
    """3.1 硬地板 load-bearing：缺口恰是先前已达成的覆盖（倒退）→ 绝不放行。"""
    out = await _run(_plan_covering(REQS[:9]), retry=1, watermark=REQS)  # REQS[9] 曾覆盖
    assert out["plan_valid"] is False, "倒退出来的缺口绝不许 degraded 放行（单调合同）"
    assert "单调" in out["plan_validation_feedback"]


async def test_full_coverage_no_gap_noise():
    out = await _run(_plan_covering(REQS), retry=1)
    assert out["plan_valid"] is True
    assert not any("gap_allowed" in d for d in out.get("degraded_reasons") or [])


async def test_gap_allowance_kill_switch(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_COVERAGE_GAP_MAX", "0")
    monkeypatch.setenv("SWARM_PLAN_COVERAGE_GAP_RATIO", "0")
    out = await _run(_plan_covering(REQS[:9]), retry=1)
    assert out["plan_valid"] is False, "两阈值归零=回到全有全无（运维泄压阀）"


# ─────────────── A9：反馈分页轮转（R67M2-T1 后=病态池安全阀） ───────────────

def _many_issues(n=900):
    # R67M2-T1 A1：全量可见帽 8000→32000——900 条 × ~45 字符 ≈ 40K 才触发安全阀分页
    return [f"需求条目 req-{i:06d} 未被任何子任务的 covers 覆盖：功能描述占位文本{i}"
            for i in range(n)]


def test_feedback_pagination_rotates_windows():
    issues = _many_issues()
    full = "\n".join(f"- {s}" for s in issues)
    p0 = _format_validation_feedback(issues, rotate=0)
    p1 = _format_validation_feedback(issues, rotate=1)
    assert len(p0) < len(full) and len(p1) < len(full), "超帽必须分页（单页显著小于全量）"
    assert p0 != p1, "不同重试轮必须轮转不同页窗（否则 LLM 永远修不了看不见的条目）"
    assert "轮转" in p0 and "轮转" in p1, "页头必须自述分页（未列出≠已解决）"
    assert "阻断级" in p0, "R67M2-T1：页头必须明示未展示页同为本轮打回的阻断级原因"
    assert "req-000000" in p0 and "req-000000" not in p1


def test_feedback_rotation_wraps_around():
    issues = _many_issues()
    p0 = _format_validation_feedback(issues, rotate=0)
    # 页数有限，rotate 大数回卷到首页
    import re
    n_pages = int(re.search(r"/(\d+)", p0).group(1))
    assert _format_validation_feedback(issues, rotate=n_pages) == p0


def test_feedback_short_list_unchanged():
    issues = ["问题一", "问题二"]
    out = _format_validation_feedback(issues, rotate=3)
    assert out == "- 问题一\n- 问题二", "未超帽时零变化（无分页噪声）"


async def test_validate_plan_rotates_feedback_by_retry_round():
    # R67M2-T1：全量帽 32000 → 800 条（×~50 字符 ≈ 40K）才进入安全阀分页
    items = [{"id": f"req-{i:06d}", "text": f"功能描述占位文本很长很长很长很长{i}",
              "kind": "functional", "source_quote": "q", "source": "d"}
             for i in range(800)]
    plan = _plan_covering([])
    o0 = await _run(plan, retry=0, items=items)
    o1 = await _run(plan, retry=1, items=items)
    assert o0["plan_validation_feedback"] != o1["plan_validation_feedback"], (
        "validate_plan 必须按 retry 轮轮转反馈页窗（round34 震荡治本）")


# ─────────────── R67M2-T1 A1：阻断级打回池全量可见（round67m2 死因治本） ───────────────

async def test_validate_plan_realistic_reject_pool_fully_visible():
    """round67m2 失明面治本：约 1.5 万字符的打回池在旧 8000 帽下会被分 2 页、
    retry=1 只展示其中一页（round67m2：3 条致命 ③b 全在未展示页 → 21/21 重产
    prompt 失明）。A1 治本后：现实打回池任何 retry 轮都【全量可见】，绝不进分页。
    （100 条 × ~148 字符 ≈ 14.8K：超旧帽 8000 → 旧码必分页；低于新帽 32000 → 全显）"""
    items = [{"id": f"req-{i:06d}", "text": f"功能描述占位文本{i}" + "长" * 110,
              "kind": "functional", "source_quote": "q", "source": "d"}
             for i in range(100)]
    plan = _plan_covering([])
    out = await _run(plan, retry=1, items=items)
    fb = out["plan_validation_feedback"]
    assert out["plan_valid"] is False
    assert len(fb) > 8000, "夹具必须真的超旧帽（否则本测试对旧码也绿=没钉住回归）"
    assert "轮转" not in fb, "现实打回池（百条内≈万字符级）绝不许分页藏阻断级 issue"
    for i in range(100):
        assert f"req-{i:06d}" in fb, f"第 {i} 条阻断级 issue 必须对重产可见（round67m2 失明面）"


# ─────────────── A9-2：外科补齐放开 MEDIUM ───────────────

async def test_topup_gate_allows_medium(monkeypatch):
    import swarm.brain.nodes as nodes
    sentinel = (_plan_covering(REQS), [])

    async def _fake_topup(*a, **k):
        return sentinel

    monkeypatch.setattr(nodes, "_targeted_coverage_topup", _fake_topup)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    from swarm.brain.nodes import _maybe_surgical_coverage_topup
    out = await _maybe_surgical_coverage_topup({
        "complexity": "medium",
        "plan_validation_feedback": "覆盖缺口",
        "replan_feedback": "",
        "plan": _plan_covering(REQS[:9]),
        "requirement_items": _items(),
    })
    assert out is sentinel, "MEDIUM 纯覆盖重试也必须走外科补齐（不重掷骰子）"


async def test_topup_gate_still_blocks_simple(monkeypatch):
    import swarm.brain.nodes as nodes

    async def _fake_topup(*a, **k):  # 不应被调到
        raise AssertionError("SIMPLE 不应走 topup")

    monkeypatch.setattr(nodes, "_targeted_coverage_topup", _fake_topup)
    from swarm.brain.nodes import _maybe_surgical_coverage_topup
    out = await _maybe_surgical_coverage_topup({
        "complexity": "simple",
        "plan_validation_feedback": "覆盖缺口",
        "replan_feedback": "",
        "plan": _plan_covering(REQS[:9]),
        "requirement_items": _items(),
    })
    assert out is None
