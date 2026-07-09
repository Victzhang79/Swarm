"""A4（2026-07-09 深读登记册·阶段0）：plan_batch 失败模块回炉重试 + 归因不遮蔽 — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §二 A4（登记册原文"无单模块重试"已更正：
批内 P6a attempts/切备/bisect/R35-C 缓存回退都在，真缺口是【跨轮】——validate 对
plan_batch_failed_modules 视而不见，plan_valid=True 直进 CONFIRM 被 can_auto_accept_plan
fail-fast 终结。U2 补齐型重试机器（_repair_retry：成功批缓存回放、只重烧失败模块）造好了
却没有入口，11/12 成功也整任务死）：
  - 治本①：validate_plan 检出失败模块 → plan_valid=False 走 D09 回灌打回 PLAN，
    U2 缓存回放成功批、只重烧失败模块；熔断复用 plan_retry_count/MAX_PLAN_RETRY，
    耗尽仍失败才落 confirm fail-fast（原终局保留）。
  - 治本②：归因遮蔽——runner 终态归因链（issues > deliver_auto_reject_reason >
    confirm_reason）里前两位皆空时报 "rejected: ultra"。confirm fail-fast 时把
    can_auto_accept_plan 的真实原因写进 deliver_auto_reject_reason（runner 链位2，
    声明键，前端已消费），confirm_reason 保持进入原因语义不变。

栈无关：抽象模块/子任务。
"""

from __future__ import annotations

from swarm.brain.graph import after_validate
from swarm.brain.nodes import confirm_plan, validate_plan
from swarm.types import Complexity, FileScope, SubTask, SubTaskDifficulty, TaskPlan


def _valid_plan():
    return TaskPlan(
        subtasks=[SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=["a"]))],
        parallel_groups=[["st-1"]],
    )


_FAILED_MODS = [{"name": "mod-broken", "files": 14, "reason": "timeout"}]


async def test_validate_fails_plan_back_on_batch_failed_modules():
    """结构合法但有整模块分解失败 → plan_valid=False 走 D09 打回 PLAN（U2 补齐型重试入口），
    而非 True 直进 CONFIRM 终结。"""
    out = await validate_plan({
        "plan": _valid_plan(),
        "task_description": "t",
        "complexity": Complexity.ULTRA,
        "plan_retry_count": 0,
        "plan_batch_failed_modules": list(_FAILED_MODS),
    })
    assert out["plan_valid"] is False, (
        "失败模块必须打回 PLAN 走补齐型重试——U2 缓存回放成功批、只重烧失败模块")
    assert any("mod-broken" in i for i in out["plan_validation_issues"])
    assert "mod-broken" in out["plan_validation_feedback"]
    assert "plan_batch_cache" not in out, "回炉必须保留 U2 缓存（清了就只能全量重拆）"
    # 打回后路由回 PLAN（熔断复用 plan_retry_count/MAX_PLAN_RETRY）
    assert after_validate({"plan_valid": False, "plan_retry_count": 0,
                           "complexity": Complexity.ULTRA}) == "plan"


async def test_validate_passes_without_failed_modules():
    """无失败模块 → 原行为不变（SIMPLE 快速路径回归锚点）。"""
    out = await validate_plan({
        "plan": _valid_plan(),
        "task_description": "t",
        "complexity": Complexity.SIMPLE,
        "plan_retry_count": 0,
        "plan_batch_failed_modules": [],
    })
    assert out["plan_valid"] is True


def test_confirm_fail_fast_surfaces_real_reason_not_confirm_reason():
    """auto_accept fail-fast 时真实死因必须进 runner 归因链（deliver_auto_reject_reason），
    不再只剩 confirm_reason='ultra' 被报成 "rejected: ultra"。"""
    out = confirm_plan({
        "plan": _valid_plan(),
        "plan_valid": True,
        "complexity": Complexity.ULTRA,
        "auto_accept": True,
        "plan_batch_failed_modules": list(_FAILED_MODS),
    })
    assert out["verification_failure"] == "plan_batch_failed"
    assert out["confirm_reason"] == "ultra"  # 进入原因语义不变（分开上报）
    _reason = out.get("deliver_auto_reject_reason") or ""
    assert "plan_batch_failed" in _reason and "mod-broken" in _reason, (
        f"真实死因未上报 runner 归因链，终态只会报 rejected: ultra；got={_reason!r}")
