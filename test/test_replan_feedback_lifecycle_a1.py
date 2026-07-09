"""A1（2026-07-09 深读登记册·阶段0 龙头）：replan_feedback 一次性消费生命周期 — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §二 A1：
  - replan_feedback 只有两处写入（handle_failure replan 分支、confirm REVISE 分支），
    全仓【无任何清空点】→ 一次 replan/REVISE 后永久粘滞。
  - 粘滞的直接后果：P1 外科补齐（_maybe_surgical_coverage_topup 要求该键为空）、
    #6 覆盖单调化（_merge_prior_covers_by_scope 并回条件）、U2 补齐缓存（_repair_retry）、
    R35-C 前向回退护栏（_allow_cache_fallback）四套保护【永久关闭】——round37b 的
    P1/P3 治本被整体架空。
  - 治本：replan_feedback 是一次性消费键。PLAN 节点成功产出新计划（失败根因已注入
    prompt 被消费）后 emit 空串；LLM 降级兜底轮（_plan_degraded）不清——下一轮 PLAN
    仍需看到根因。

栈无关：全部用抽象 req/子任务，无任何语言/框架/领域词汇。
"""

from __future__ import annotations

import os

from swarm.brain.nodes import _maybe_surgical_coverage_topup, plan
from swarm.types import Complexity, FileScope, SubTask, SubTaskDifficulty, TaskPlan

REQ_A = "req-aaaa1111"
REQ_B = "req-bbbb2222"


def _items():
    return [
        {"id": REQ_A, "text": "系统支持条目一", "kind": "functional",
         "source_quote": "一", "source": "description"},
        {"id": REQ_B, "text": "系统支持条目二", "kind": "data",
         "source_quote": "二", "source": "description"},
    ]


def _st(sid, writable=None, covers=None, depends_on=None, desc="do"):
    return SubTask(
        id=sid,
        description=desc,
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=list(writable or []), readable=[]),
        covers=list(covers or []),
        depends_on=list(depends_on or []),
    )


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """捕获 prompt 并回吐固定 JSON 的假 LLM（既有 D09/覆盖测试同款形态）。"""

    def __init__(self, content="{}"):
        self._content = content
        self.captured: list[str] = []

    async def ainvoke(self, messages):
        self.captured.append(messages[-1]["content"])
        return _Resp(self._content)


_PLAN_JSON = (
    '{"subtasks":[{"id":"st-1","description":"x",'
    '"scope":{"writable":["a"],"readable":[]},"covers":["%s"]}],'
    '"parallel_groups":[["st-1"]]}' % REQ_A
)

_FEEDBACK = "上轮执行失败根因：子任务依赖悬空"


def _clean_env():
    os.environ.pop("SWARM_PLAN_COVERAGE_TOPUP", None)


# ─────────────────── PLAN 成功产出 → 清空（一次性消费）───────────────────

async def test_medium_plan_consumes_then_clears_replan_feedback(monkeypatch):
    """主路径：replan 重入的失败根因注入 prompt（消费）后，patch 必须清空该键。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    fake = _FakeLLM(_PLAN_JSON)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    out = await plan({
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "requirement_items": _items(),
        "replan_feedback": _FEEDBACK,
        "replan_count": 1,
    })
    assert _FEEDBACK in fake.captured[0], "replan 根因必须注入 prompt（消费）"
    assert out.get("replan_feedback", None) == "", (
        "PLAN 成功产出后必须清空 replan_feedback——否则永久粘滞，"
        "P1 外科补齐/#6 覆盖单调化/U2 缓存/R35-C 回放四套保护整体架空")


async def test_simple_plan_clears_replan_feedback():
    """SIMPLE 快速路径（确定性构造无 LLM）：同样清空，防粘滞键跨复杂度存活。"""
    _clean_env()
    out = await plan({
        "task_description": "small fix",
        "complexity": Complexity.SIMPLE,
        "affected_files": ["a"],
        "replan_feedback": _FEEDBACK,
    })
    assert out.get("replan_feedback", None) == ""


async def test_degraded_plan_keeps_replan_feedback(monkeypatch):
    """LLM 降级兜底轮（JSON 解析失败→空 scope 假计划）不清——下一轮 PLAN 仍需看到根因。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    fake = _FakeLLM("这不是 JSON")
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    out = await plan({
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "replan_feedback": _FEEDBACK,
    })
    assert out.get("plan_generation_failed") is True
    assert (out.get("replan_feedback") or "") == _FEEDBACK, (
        "降级兜底轮必须保留失败根因供下一轮真规划消费")


# ─────────────────── 回归：一次 replan 后保护重新生效 ───────────────────

async def test_protections_reengage_after_one_replan_round(monkeypatch):
    """A1 核心回归：replan 轮成功产出后，后续【纯覆盖重试】必须重新命中 P1 外科补齐。

    模拟完整生命周期：执行失败(handle_failure 写入 replan_feedback) → PLAN replan 轮
    成功产出(patch 清空) → 覆盖闸未过(plan_validation_feedback 非空) → 下一轮 PLAN 的
    P1 闸门 _maybe_surgical_coverage_topup 必须启用（返回非 None），而非因粘滞键
    永久回退全量重拆。
    """
    _clean_env()
    import swarm.brain.nodes as nodes
    fake = _FakeLLM(_PLAN_JSON)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    # ① 执行失败后 handle_failure 注入 replan_feedback（写入点行为，直接造 state）
    state = {
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "requirement_items": _items(),
        "replan_feedback": _FEEDBACK,
        "replan_count": 1,
    }
    # ② replan 轮：PLAN 成功产出（feedback 被消费）
    patch = await plan(state)
    state = {**state, **patch}
    # ③ 覆盖闸未过 → 纯覆盖重试轮。P1 闸门必须启用。
    state["complexity"] = Complexity.ULTRA  # P1 收窄到 ULTRA（黑洞场景）
    state["plan_validation_feedback"] = f"- 未覆盖: {REQ_B}"
    topup_fake = _FakeLLM(
        '{"assignments":[{"req_id":"%s","subtask_id":"st-1"}],'
        '"baseline_covered":[]}' % REQ_B)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: topup_fake)
    got = await _maybe_surgical_coverage_topup(state)
    assert got is not None, (
        "一次 replan 后 replan_feedback 若粘滞，P1 外科补齐永久关闭=round37b 治本被架空；"
        "清空后纯覆盖重试必须重新走外科路径")


async def test_cache_replay_guard_reengages_after_one_replan_round(monkeypatch):
    """A1 配套回归：replan 轮成功产出后，U2/R35-C 缓存启用条件（replan_feedback 空）恢复。

    不跑分批全链路，直接断言 patch 后 state 的启用条件表达式恢复真值——与
    nodes/__init__.py 中 _repair_retry / _allow_cache_fallback 的判定表达式同构。
    """
    _clean_env()
    import swarm.brain.nodes as nodes
    fake = _FakeLLM(_PLAN_JSON)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    state = {
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "replan_feedback": _FEEDBACK,
    }
    patch = await plan(state)
    state = {**state, **patch}
    assert not (state.get("replan_feedback") or "").strip(), (
        "replan 轮产出后 U2 缓存/R35-C 回放护栏的启用条件必须恢复")
