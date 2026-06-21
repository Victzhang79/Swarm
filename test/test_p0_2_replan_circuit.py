"""P0-2 回归测试：规划失败熔断 + replan 携带失败原因 + recursion_limit 显式注入。

背景（task 0f93f1fc）：
- Brain graph 用 LangGraph 默认 recursion_limit=25，规划循环+多子任务+replan 重入
  撞穿后抛 GRAPH_RECURSION_LIMIT 硬崩，用户只看到框架报错。
- handle_failure 的 replan 无次数上限，replan→PLAN→（同样的坏计划）→再失败 无限循环。
- replan 不携带失败原因，LLM 看不到根因 → 原样重生成同一个坏计划。

修复：
1. brain_graph_config 显式设 recursion_limit=BRAIN_RECURSION_LIMIT(默认 50)。
2. handle_failure replan 计数 + 超 max_retries 升级人工(escalate)。
3. replan 时把 reasoning 写入 replan_feedback；PLAN 节点读取并注入上下文。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


def _plan():
    return TaskPlan(
        subtasks=[
            SubTask(
                id="st-1", description="d",
                difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                scope=FileScope(writable=["a.py"]),
            )
        ],
        parallel_groups=[["st-1"]],
    )


def _state(**over):
    s = {
        "complexity": Complexity.MEDIUM,  # 非 SIMPLE → 走 LLM 分析路径
        "plan": _plan(),
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": WorkerOutput(subtask_id="st-1", diff="", summary="x", l1_passed=False)},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    s.update(over)
    return s


class _FakeResp:
    def __init__(self, content): self.content = content


def _fake_llm_returning(strategy, reasoning="scope 配置冲突，依赖悬空"):
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp('{"strategy":"%s","reasoning":"%s"}' % (strategy, reasoning))
    return lambda: _L()


# ── replan 熔断 ───────────────────────────────────────────
def test_replan_first_time_proceeds_with_feedback():
    """首次 replan：触发重规划 + 携带失败原因。"""
    with patch.object(nodes, "_get_brain_llm", _fake_llm_returning("replan")):
        out = asyncio.run(nodes.handle_failure(_state()))
    assert out.get("failure_strategy") == "replan", out.get("failure_strategy")
    assert out.get("replan_count") == 1
    assert out.get("replan_feedback"), "replan 应携带失败原因供 PLAN 参考"
    print("  ✅ replan 首次：触发重规划 + 携带失败原因")


def test_replan_circuit_breaks_at_limit():
    """replan 累计超 max_retries(默认 2) → 升级人工 escalate，不再无限重规划。"""
    from swarm.config.settings import get_config
    max_replan = get_config().model.max_retries
    with patch.object(nodes, "_get_brain_llm", _fake_llm_returning("replan")):
        # 已 replan max_replan 次，本次为第 max_replan+1 次 → 熔断
        out = asyncio.run(nodes.handle_failure(_state(replan_count=max_replan)))
    assert out.get("failure_strategy") == "escalate", out.get("failure_strategy")
    assert out.get("failure_escalated") is True
    assert out.get("l2_passed") is False
    print(f"  ✅ replan 熔断：超 {max_replan} 次升级人工（避免无限重规划撞穿 recursion_limit）")


# ── PLAN 注入 replan_feedback ─────────────────────────────
def test_plan_injects_replan_feedback():
    """PLAN 重入时把 replan_feedback 拼进传给 LLM 的 prompt。"""
    captured = {}

    class _L:
        async def ainvoke(self, msgs):
            captured["user"] = msgs[-1]["content"]
            return _FakeResp('{"subtasks":[{"id":"st-1","description":"d",'
                             '"difficulty":"medium","modality":"text",'
                             '"scope":{"writable":["a.py"],"readable":[]},'
                             '"acceptance_criteria":["x"]}],"parallel_groups":[["st-1"]]}')

    feedback = "上轮 st-2 依赖悬空 + scope 写权限缺失"
    with patch.object(nodes, "_get_brain_llm", lambda: _L()):
        asyncio.run(nodes.plan({
            "task_description": "做点事",
            "complexity": Complexity.MEDIUM,
            "knowledge_context": {},
            "replan_count": 1,
            "replan_feedback": feedback,
        }))
    assert feedback in captured.get("user", ""), "PLAN prompt 应包含上轮失败原因"
    print("  ✅ PLAN 重入：上轮失败原因已注入 LLM prompt")


# ── recursion_limit 显式注入 ──────────────────────────────
def test_brain_graph_config_sets_recursion_limit():
    from swarm.tracing import (
        BRAIN_RECURSION_LIMIT,
        brain_graph_config,
        resolve_brain_recursion_limit,
    )
    # RUN21 修复后：不带 complexity/subtask_count（新任务首轮 invoke 的真实情形）时，
    # 不再落低 floor，而是按最坏情况 ultra 兜底（避免大 ultra 任务全程跑 50 撞穿）。
    cfg = brain_graph_config(task_id="t1", project_id="p1", thread_id="th1")
    assert cfg.get("recursion_limit") == resolve_brain_recursion_limit(None, None), cfg
    assert cfg.get("recursion_limit") >= 300, f"未知规模应按 ultra 兜底(>=300)，实得 {cfg.get('recursion_limit')}"
    assert BRAIN_RECURSION_LIMIT >= 50, f"floor 应 >=50，实际 {BRAIN_RECURSION_LIMIT}"
    print(f"  ✅ brain_graph_config: 未知规模 recursion_limit={cfg.get('recursion_limit')}（RUN21 修复，不再落 50）")


if __name__ == "__main__":
    tests = [
        test_replan_first_time_proceeds_with_feedback,
        test_replan_circuit_breaks_at_limit,
        test_plan_injects_replan_feedback,
        test_brain_graph_config_sets_recursion_limit,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n=== P0-2 规划熔断/recursion: {passed}/{passed+failed} passed ===")
    import sys
    sys.exit(1 if failed else 0)
