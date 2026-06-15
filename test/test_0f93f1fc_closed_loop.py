"""端到端闭环回归（task 0f93f1fc）：elaborate → validate_plan_structure。

复现并验证整条规划链路的修复：
- 修复前：st-1 超预算二次拆分 → st-2 依赖悬空 → 结构校验失败 → 规划死循环 →
  recursion_limit 撞穿崩溃。
- 修复后：elaborate 重映射下游依赖(P0-1) + scope 归一(P1-1)，validate_plan_structure
  应通过，无悬空依赖、无环。

这是不依赖沙箱/在线模型的确定性闭环验证（覆盖 P0-1 主路径）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.brain import planning_nodes as P
from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _sub(sid, *, est=0, acc=None, deps=None, writable=None, readable=None, create=None):
    return SubTask(
        id=sid, description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [], readable=readable or [], create_files=create or []),
        acceptance_criteria=acc or [], est_context_tokens=est, depends_on=deps or [],
    )


def test_0f93f1fc_plan_loop_resolved_end_to_end():
    """精确复现 task 0f93f1fc 的规划形态，验证 elaborate 后结构校验通过（不再死循环）。"""
    budget = P._context_budget()

    class _R:
        # st-1 超预算 → mock LLM 拆成 2 个子任务（模拟 ELABORATE 二次拆分）
        content = ('{"subtasks":[{"description":"建 NumberUtils","acceptance_criteria":["a"],"est_context_tokens":40000},'
                   '{"description":"建 NumberUtilsTest","acceptance_criteria":["b"],"est_context_tokens":40000}]}')

    class _L:
        async def ainvoke(self, _m): return _R()

    _orig = P._get_brain_llm
    P._get_brain_llm = lambda: _L()
    try:
        # task 0f93f1fc 原始计划：st-1(建 NumberUtils+测试, 超预算) ← st-2(改 StringUtils 委托)
        plan = TaskPlan(
            subtasks=[
                _sub("st-1", est=budget + 50_000, acc=["x"],
                     create=["NumberUtils.java", "NumberUtilsTest.java"]),
                _sub("st-2", est=14_000, acc=["y"], deps=["st-1"],
                     readable=["NumberUtils.java"], writable=["StringUtils.java"]),
            ],
            parallel_groups=[["st-1"], ["st-2"]],
        )
        # 无 project_id → P2-1 跳过（不影响本验证），专注 P0-1 + P1-1
        out = asyncio.run(P.elaborate({"plan": plan, "task_id": "", "project_id": ""}))
        new_plan = out.get("plan")
        assert new_plan is not None

        # 结构校验：修复前这里必报"子任务 st-2 依赖未知任务 st-1"
        result = validate_plan_structure(new_plan)
        assert result.valid, f"结构校验应通过，但失败: {result.issues}"

        # 显式确认：无悬空依赖
        ids = {s.id for s in new_plan.subtasks}
        for s in new_plan.subtasks:
            for d in (s.depends_on or []):
                assert d in ids, f"悬空依赖 {s.id}->{d}"
        # st-2 依赖已重映射到拆分后的尾节点
        st2 = next(s for s in new_plan.subtasks if s.id == "st-2")
        assert st2.depends_on and all(d != "st-1" for d in st2.depends_on), \
            f"st-2 不应再依赖被拆掉的 st-1: {st2.depends_on}"
        print("  ✅ 闭环: task 0f93f1fc 规划形态 — elaborate 后结构校验通过，无悬空依赖")
        print(f"     最终子任务: {sorted(ids)}, st-2.depends_on={st2.depends_on}")
    finally:
        P._get_brain_llm = _orig


if __name__ == "__main__":
    try:
        test_0f93f1fc_plan_loop_resolved_end_to_end()
        print("\n=== 闭环验证: 1/1 passed ===")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        import sys
        sys.exit(1)
