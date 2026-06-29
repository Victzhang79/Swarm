"""P0-3 回归测试：confirm_plan 不放行非法计划。

背景（task 0f93f1fc）：计划自动校验失败 4 次后路由到 CONFIRM，但 auto_accept 模式
无条件返回 ACCEPT，把已知非法计划送进 DISPATCH，必然 scope 冲突 + 悬空依赖失败。

修复（产品决策 Q2）：
- plan_valid=True  → auto_accept 正常放行。
- plan_valid=False + auto_accept（纯自动无人监听）→ 降级 fail-fast(REJECT)。
- plan_valid=False + 非 auto_accept（有人监听）→ interrupt 等人工（出选项+输入框）。

并修 P2-2：confirm 文案/reason 按进入原因区分，不再无条件"ultra 复杂度"。
"""
from __future__ import annotations

import swarm.brain.nodes as nodes
from swarm.types import Complexity, FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan
from swarm.types import HumanDecision


def _plan():
    return TaskPlan(
        subtasks=[SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                          modality=SubTaskModality.TEXT, scope=FileScope(writable=["a.py"]))],
        parallel_groups=[["st-1"]],
    )


def test_confirm_valid_plan_auto_accepts():
    out = nodes.confirm_plan({
        "auto_accept": True, "plan_valid": True, "plan": _plan(),
        "complexity": Complexity.ULTRA, "task_id": "t1",
    })
    assert out["human_decision"] == HumanDecision.ACCEPT
    print("  ✅ confirm: 合法计划 auto_accept 正常放行")


def test_confirm_invalid_plan_auto_failfast():
    """非法计划 + auto_accept（纯自动）→ fail-fast REJECT，不放行。"""
    out = nodes.confirm_plan({
        "auto_accept": True, "plan_valid": False, "plan": _plan(),
        "complexity": Complexity.MEDIUM, "task_id": "t1",
        "plan_validation_issues": ["子任务 st-2 依赖未知任务 st-1"],
    })
    assert out["human_decision"] == HumanDecision.REJECT, out
    assert out.get("verification_failure") == "plan_invalid"
    assert out.get("confirm_reason") == "validation_failed"
    print("  ✅ confirm: 非法计划 auto_accept 降级 fail-fast(REJECT)，不放行（task 0f93f1fc 闸门）")


def test_confirm_invalid_plan_interrupts_for_human():
    """非法计划 + 非 auto_accept（有人监听）→ interrupt 等人工。"""
    # interrupt() 在无 checkpointer 上下文会抛 GraphInterrupt（或类似），
    # 我们只验证它确实尝试中断（不返回 ACCEPT），而非静默放行。
    raised = False
    try:
        nodes.confirm_plan({
            "auto_accept": False, "plan_valid": False, "plan": _plan(),
            "complexity": Complexity.MEDIUM, "task_id": "t1",
            "plan_validation_issues": ["依赖悬空"],
        })
    except Exception as e:
        # langgraph 的 interrupt 在非 graph 执行上下文会抛 GraphInterrupt/Empty 等
        raised = True
        print(f"  ✅ confirm: 非法计划+有人监听 → 触发 interrupt 等人工 (raised {type(e).__name__})")
    assert raised, "非 auto_accept 的非法计划应 interrupt 等人工，而非直接返回"


def test_confirm_reason_text_distinguishes():
    """P2-2：reason 文案区分 validation_failed / ultra / manual_confirm（不再写死 ultra）。"""
    # validation_failed 优先级最高（即便 complexity=ultra）
    out = nodes.confirm_plan({
        "auto_accept": True, "plan_valid": False, "plan": _plan(),
        "complexity": Complexity.ULTRA, "task_id": "t1",
        "plan_validation_issues": ["x"],
    })
    assert out.get("confirm_reason") == "validation_failed", "校验失败 reason 应优先于 ultra"
    print("  ✅ confirm: reason 按进入原因区分（P2-2 文案修正）")


def test_confirm_tech_design_generation_failed_failfast():
    """#22：tech_design 整体生成失败(LLM 异常,file_plan 为空) + auto_accept → fail-fast，不放行。

    即便 plan_valid=True（兜底空计划可能被判合法），整体设计失败也绝不能静默 auto_accept。
    """
    out = nodes.confirm_plan({
        "auto_accept": True, "plan_valid": True, "plan": _plan(),
        "complexity": Complexity.ULTRA, "task_id": "t1",
        "tech_design_generation_failed": True,
    })
    assert out["human_decision"] == HumanDecision.REJECT, out
    print("  ✅ confirm: tech_design 整体生成失败 → fail-fast，不静默放行（#22 闸门）")


if __name__ == "__main__":
    tests = [
        test_confirm_valid_plan_auto_accepts,
        test_confirm_invalid_plan_auto_failfast,
        test_confirm_invalid_plan_interrupts_for_human,
        test_confirm_reason_text_distinguishes,
        test_confirm_tech_design_generation_failed_failfast,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t(); passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n=== P0-3 confirm 闸门: {passed}/{passed+failed} passed ===")
    import sys
    sys.exit(1 if failed else 0)
