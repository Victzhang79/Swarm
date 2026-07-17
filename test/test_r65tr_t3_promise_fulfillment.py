"""R65TR-T3：重派承诺飞行中兑现。

治后回放（963d78da）#71 终态账逮到的 2 个软掉账的中段根因定案：
- st-39-1：22:57 定向恢复授 ruoyi-admin/pom.xml 写权后，_grant_module_pom_writable
  的"串到既有 owner 后面"取【计划序第一个 scope 含该 pom 者】（st-29-1，从未派发、
  困在 st-2 连坐闭包的对等 modify 写者）→ 授权即死刑，1 小时 ~10 个派发窗口零日志。
  设计意图是串到已 DONE 的脚手架 creator 后面；对等 modify 写者不该反向锁死被恢复者。
- st-23-1：C9 边含 st-2 传递下游生产者（生产者活着时等待诚实，但等待面零观测）。

治本：
- P1 grant 串边方向运行态修正——owner 是真 creator（create_files 含该清单）保持
  grantee→owner（注册序）；对等 modify 写者反转 owner→grantee（被恢复者先行，
  防环命中则不加边，并发写交 E3 写集锁+MERGE）+ 必打日志。
- P2 重派承诺账龄——曾派发过（dispatch_totals>0）却连续 N 个派发窗口未被选中的
  子任务，WARNING 点名未满足依赖（#71 终态账提前到飞行中）。
"""

from __future__ import annotations

import logging

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


def _plan(owner_creates_pom: bool):
    owner_scope = (
        FileScope(create_files=["m/pom.xml"], writable=["m/src/Login.java"])
        if owner_creates_pom else
        FileScope(create_files=["m/src/Login.java"], writable=["m/pom.xml"])
    )
    return TaskPlan(subtasks=[
        SubTask(id="st-peer", description="2FA 登录（对等写者/或 creator）",
                difficulty=SubTaskDifficulty.MEDIUM, scope=owner_scope),
        SubTask(id="st-victim", description="快照控制器（恢复重派者）",
                difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=["m/src/SnapCtrl.java"])),
    ], parallel_groups=[["st-peer", "st-victim"]])


# ── P1：grant 串边方向 ────────────────────────────────────────────────


def test_grant_reverses_edge_for_peer_modify_writer():
    """owner 只是对等 modify 写者 → 绝不把被恢复者挂它后面；反转为 owner→grantee。"""
    from swarm.brain.nodes.planning_core import _grant_module_pom_writable

    plan = _plan(owner_creates_pom=False)
    granted = _grant_module_pom_writable(plan, ["st-victim"])
    assert granted.get("st-victim") == "m/pom.xml"
    victim = next(s for s in plan.subtasks if s.id == "st-victim")
    peer = next(s for s in plan.subtasks if s.id == "st-peer")
    assert "st-peer" not in (victim.depends_on or []), \
        "被恢复者绝不能挂到对等 modify 写者后面（963d78da st-39-1→st-29-1 授权即死刑实锤）"
    assert "st-victim" in (peer.depends_on or []), \
        "序不变量仍要满足：反转为对等写者靠后"


def test_grant_keeps_creator_order():
    """owner 是真 creator（脚手架建 pom）→ 保持 grantee→owner 注册序（原语义）。"""
    from swarm.brain.nodes.planning_core import _grant_module_pom_writable

    plan = _plan(owner_creates_pom=True)
    _grant_module_pom_writable(plan, ["st-victim"])
    victim = next(s for s in plan.subtasks if s.id == "st-victim")
    assert "st-peer" in (victim.depends_on or []), "creator 序（pom 先在）必须保留"


def test_grant_reversal_cycle_guard_skips_edge():
    """反转会成环（grantee 已传递依赖对等写者）→ 不加边、不炸（并发交写集锁+MERGE）。"""
    from swarm.brain.nodes.planning_core import _grant_module_pom_writable

    plan = _plan(owner_creates_pom=False)
    victim = next(s for s in plan.subtasks if s.id == "st-victim")
    victim.depends_on = ["st-peer"]  # 既有边：反转即成环
    _grant_module_pom_writable(plan, ["st-victim"])
    peer = next(s for s in plan.subtasks if s.id == "st-peer")
    assert "st-victim" not in (peer.depends_on or []), "防环守卫必须拦下反转边"


# ── P2：重派承诺账龄 ──────────────────────────────────────────────────


def _age_plan():
    return TaskPlan(subtasks=[
        SubTask(id="st-p", description="生产者", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=["m/src/P.java"])),
        SubTask(id="st-c", description="消费者（重派承诺者）", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=["m/src/C.java"]), depends_on=["st-p"]),
    ], parallel_groups=[["st-p"]])


def test_promise_age_warns_with_unmet_deps(caplog):
    from swarm.brain.nodes.dispatch import (
        _REDISPATCH_AGE_WARN_WINDOWS, _track_redispatch_promise_ages,
    )

    plan = _age_plan()
    ages: dict = {}
    with caplog.at_level(logging.WARNING):
        for _ in range(_REDISPATCH_AGE_WARN_WINDOWS):
            ages = _track_redispatch_promise_ages(
                ages, dispatch_remaining=["st-c"], selected_ids=set(),
                abandoned=set(), plan_obj=plan, completed_ids=set(),
                dispatch_totals={"st-c": 1},
            )
    assert ages.get("st-c") == _REDISPATCH_AGE_WARN_WINDOWS
    warns = [r.getMessage() for r in caplog.records if "重派承诺账龄" in r.getMessage()]
    assert len(warns) == 1, f"账龄告警应在阈值整倍数打且不刷屏: {warns}"
    assert "st-c" in warns[0] and "st-p" in warns[0], \
        f"必须点名子任务与未满足依赖: {warns[0]}"


def test_promise_age_resets_on_selection():
    from swarm.brain.nodes.dispatch import _track_redispatch_promise_ages

    plan = _age_plan()
    ages = _track_redispatch_promise_ages(
        {}, dispatch_remaining=["st-c"], selected_ids=set(), abandoned=set(),
        plan_obj=plan, completed_ids=set(), dispatch_totals={"st-c": 1})
    assert ages.get("st-c") == 1
    ages = _track_redispatch_promise_ages(
        ages, dispatch_remaining=["st-c"], selected_ids={"st-c"}, abandoned=set(),
        plan_obj=plan, completed_ids=set(), dispatch_totals={"st-c": 1})
    assert "st-c" not in ages, "被选中派发即兑现，账龄清零"


def test_promise_age_ignores_never_dispatched():
    from swarm.brain.nodes.dispatch import _track_redispatch_promise_ages

    plan = _age_plan()
    ages = _track_redispatch_promise_ages(
        {}, dispatch_remaining=["st-c"], selected_ids=set(), abandoned=set(),
        plan_obj=plan, completed_ids=set(), dispatch_totals={})
    assert "st-c" not in ages, "从未派发过=无承诺，不入账龄（正常依赖等待非本观测对象）"


def test_brainstate_declares_wait_windows_key():
    """LangGraph 未声明键=静默丢弃（批4a 实证）——state 键必须声明。"""
    from swarm.brain.state import BrainState
    assert "redispatch_wait_windows" in BrainState.__annotations__


# ── 对抗双复核整改锁（hunter 2×HIGH+MED / reviewer HIGH） ─────────────


def test_cowriter_serialization_edge_is_soft():
    """共写序边=软（2×HIGH）：反转边判硬会让 grantee 之死把健康共写者拖进
    revert 级联（含阶梯三无 completed_ids 保护路径=已完成产物被 git 还原）。"""
    from swarm.types import edge_is_soft

    plan = _plan(owner_creates_pom=False)
    from swarm.brain.nodes.planning_core import _grant_module_pom_writable
    _grant_module_pom_writable(plan, ["st-victim"])
    owner = next(s for s in plan.subtasks if s.id == "st-peer")
    victim = next(s for s in plan.subtasks if s.id == "st-victim")
    # 反转边 owner→victim：双方共写 m/pom.xml、零生产关系 → 软
    assert edge_is_soft(owner, victim) is True, \
        "共写序边必须判软（只序不连坐）"


def test_transitive_abandon_spares_cowriter_on_reversed_edge():
    """hunter F1 复现锁：grantee 被弃时，经反转共写序边挂着的 owner 绝不连坐
    ——即使调用方（阶梯三 revert 路径）不传 completed_ids。"""
    from swarm.brain.nodes.planning_core import _grant_module_pom_writable, _transitive_abandon

    plan = _plan(owner_creates_pom=False)
    _grant_module_pom_writable(plan, ["st-victim"])
    closed = _transitive_abandon(plan.subtasks, {"st-victim"})
    assert closed == {"st-victim"}, \
        f"共写序边不得传播放弃（治 A 病造 B 病面）: {closed}"


def test_multi_grantee_peers_never_own_each_other():
    """hunter F3 复现锁：同批多受害者互不为 owner——首列受害者不得被串到兄弟后面。"""
    from swarm.brain.nodes.planning_core import _grant_module_pom_writable

    plan = TaskPlan(subtasks=[
        SubTask(id=f"st-{x}", description=f"受害者{x}", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=[f"m/src/{x}.java"]))
        for x in ("a", "b", "c")
    ], parallel_groups=[["st-a", "st-b", "st-c"]])
    granted = _grant_module_pom_writable(plan, ["st-a", "st-b", "st-c"])
    assert set(granted) == {"st-a", "st-b", "st-c"}
    for s in plan.subtasks:
        assert not (set(s.depends_on or []) & {"st-a", "st-b", "st-c"}), \
            f"批内对等者不得互为 owner 串边: {s.id}→{s.depends_on}"


def test_wait_windows_registered_in_lifecycle():
    """hunter F5：新记账键必须登记生命周期表（登记册纪律）。"""
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE
    assert "redispatch_wait_windows" in ACCOUNTING_KEY_LIFECYCLE


def test_wait_windows_pruned_on_replan_signature_change():
    """hunter F5：replan 重编号 id 复用是默认情形——签名变即剪账龄，绝不继承陈旧账。"""
    from swarm.brain.nodes import _surgical_replan_reset

    old = TaskPlan(subtasks=[
        SubTask(id="st-1", description="旧语义", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=["a/Old.java"]))], parallel_groups=[["st-1"]])
    new = TaskPlan(subtasks=[
        SubTask(id="st-1", description="全新语义", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=["b/New.java"]))], parallel_groups=[["st-1"]])
    out = _surgical_replan_reset({}, old, new, old_wait_windows={"st-1": 7})
    assert out.get("redispatch_wait_windows") == {}, \
        f"签名变必须剪账龄: {out.get('redispatch_wait_windows')}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
