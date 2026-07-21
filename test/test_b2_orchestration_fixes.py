"""B2 编排/状态机修复红测试：#74 SIMPLE L2 纯删除/AUDIT / #76 项目缺失 fail-fast / #78 对抗打回不误路由。"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.brain.nodes.recovery import _is_missing_dependency_failure
from swarm.brain.nodes.verify import verify_l2
from swarm.brain.scheduler import _project_exec_admission
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    TaskHarness,
    TaskIntent,
    TaskPlan,
    WorkerOutput,
)

_DELETE_DIFF = (
    "diff --git a/foo/Bar.java b/foo/Bar.java\n"
    "deleted file mode 100644\n"
    "--- a/foo/Bar.java\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-line1\n"
    "-line2\n"
)


def _simple_plan(intent=TaskIntent.MODIFY):
    st = SubTask(id="t1", description="d", intent=intent,
                 scope=FileScope(delete_files=["foo/Bar.java"]),
                 harness=TaskHarness(language="java"))
    return TaskPlan(subtasks=[st])


# ── #74 SIMPLE L2 纯删除/AUDIT 合法放行 ──────────────────────────────────────
def test_74_simple_pure_delete_passes_l2():
    state = {
        "complexity": Complexity.SIMPLE, "merged_diff": _DELETE_DIFF,
        "project_id": "p", "plan": _simple_plan(), "task_description": "删除 foo/Bar.java",
        "subtask_results": {"t1": WorkerOutput(subtask_id="t1", diff=_DELETE_DIFF, l1_passed=True, summary="ok")},
    }
    out = asyncio.run(verify_l2(state))
    assert out.get("l2_passed") is True, "SIMPLE 纯删除 diff 被 L2 假失败（#74）"


def test_74_simple_all_audit_empty_diff_passes():
    state = {
        "complexity": Complexity.SIMPLE, "merged_diff": "",
        "project_id": "p", "plan": _simple_plan(intent=TaskIntent.AUDIT),
        "task_description": "审计", "subtask_results": {
            "t1": WorkerOutput(subtask_id="t1", diff="", l1_passed=True, summary="ok")},
    }
    out = asyncio.run(verify_l2(state))
    assert out.get("l2_passed") is True, "全 AUDIT 空 diff 被 L2 假失败（#74）"


def test_74_simple_multi_subtask_noop_sibling_fails():
    # 复核 CONFIRMED HIGH：多子任务 SIMPLE——一个 MODIFY 子任务静默空产出（l1_passed=True）＋一个纯删除
    # 兄弟贡献 - 行 → merged 只有删除段。必须 l2 失败（MODIFY 兄弟未产出预期），绝不被删除段误放成 DONE。
    st_mod = SubTask(id="mod", description="d", intent=TaskIntent.MODIFY,
                     scope=FileScope(writable=["a.java"]), harness=TaskHarness(language="java"))
    st_del = SubTask(id="del", description="d", intent=TaskIntent.MODIFY,
                     scope=FileScope(delete_files=["b.java"]), harness=TaskHarness(language="java"))
    state = {
        "complexity": Complexity.SIMPLE, "merged_diff": _DELETE_DIFF,
        "project_id": "p", "plan": TaskPlan(subtasks=[st_mod, st_del]),
        "task_description": "改+删", "subtask_results": {
            "mod": WorkerOutput(subtask_id="mod", diff="", l1_passed=True, summary="ok"),
            "del": WorkerOutput(subtask_id="del", diff=_DELETE_DIFF, l1_passed=True, summary="ok")},
    }
    out = asyncio.run(verify_l2(state))
    assert out.get("l2_passed") is not True, "MODIFY 兄弟空产出被删除段掩盖成假 DONE（#74 scope-blind）"


def test_74_simple_empty_non_audit_still_fails():
    # 收紧不得误放：普通 MODIFY 计划、空产出仍应失败（空 diff 假 DONE 防线不破）。
    state = {
        "complexity": Complexity.SIMPLE, "merged_diff": "",
        "project_id": "p", "plan": _simple_plan(intent=TaskIntent.MODIFY),
        "task_description": "改", "subtask_results": {
            "t1": WorkerOutput(subtask_id="t1", diff="", l1_passed=True, summary="ok")},
    }
    out = asyncio.run(verify_l2(state))
    assert out.get("l2_passed") is not True, "空产出普通任务被误放（#74 过宽）"


# ── #76 项目记录缺失 fail-fast，读异常仍保守 ready ────────────────────────────
def test_76_project_record_missing_fails_fast():
    with patch("swarm.project.store.get_project", return_value=None):
        assert _project_exec_admission("proj-x") == "error"


def test_76_project_read_exception_stays_ready():
    def _boom(_):
        raise RuntimeError("DB 抖动")
    with patch("swarm.project.store.get_project", side_effect=_boom):
        assert _project_exec_admission("proj-x") == "ready"


def test_76_project_ready_status_admits():
    with patch("swarm.project.store.get_project", return_value={"status": "READY"}):
        assert _project_exec_admission("proj-x") == "ready"


# ── #78 对抗复核打回不误路由进 Maven 补依赖臂 ────────────────────────────────
def test_78_adversarial_critique_not_missing_dep():
    res = {"st-1": {"l1_passed": False, "l1_details": {
        "adversarial_critique": "引用了找不到符号的类型，程序包组织混乱",
        "error": "adversarial reject"}}}
    assert _is_missing_dependency_failure(res, ["st-1"]) is False, "对抗打回被误判缺依赖（#78）"


def test_78_real_missing_dep_still_detected():
    res = {"st-1": {"l1_passed": False, "l1_details": {
        "error": "package okhttp3 does not exist", "build_output": "cannot find symbol"}}}
    assert _is_missing_dependency_failure(res, ["st-1"]) is True, "真缺依赖被漏判（#78 过宽）"


# ── #109 软掉账兑现（账龄≥硬阈值→掉账+传递放弃下游，fail-honest PARTIAL）─────────────
def _mk(sid, *, create=None, depends=None):
    return SubTask(id=sid, description="d",
                   scope=FileScope(create_files=create or [f"{sid}.java"]),
                   harness=TaskHarness(language="java"), depends_on=depends or [])


def test_109_soft_drop_aged_dep_blocked_cascades_downstream():
    # A3 死型：st-mid 依赖 st-up（上游卡死未完成），st-mid 重派承诺账龄超硬阈值=上游不可兑现 → 软掉账
    # + 传递放弃其下游 st-leaf。st-other 独立不受影响。
    from swarm.brain.nodes.dispatch import _soft_drop_aged_promises
    plan = TaskPlan(subtasks=[_mk("st-up"), _mk("st-mid", depends=["st-up"]),
                              _mk("st-leaf", depends=["st-mid"]), _mk("st-other")])
    ages = {"st-mid": 24, "st-other": 3}
    dr = ["st-mid", "st-leaf", "st-other"]
    new_dr, new_ab, new_ages, aged = _soft_drop_aged_promises(ages, dr, [], plan, set(), 24)
    assert aged == ["st-mid"], aged   # 有未满足依赖 st-up + 账龄超阈值
    assert "st-mid" in new_ab and "st-leaf" in new_ab, f"下游未传递放弃: {new_ab}"
    assert "st-other" in new_dr and "st-other" not in new_ab, "误伤独立子任务"
    assert "st-mid" not in new_dr and "st-leaf" not in new_dr, "掉账后未移出 remaining"
    assert "st-mid" not in new_ages, "掉账后账龄未清"


def test_109_no_unmet_dep_not_dropped_concurrency_starvation():
    # ★复核 PLAUSIBLE 整改★：无未满足依赖却账龄超阈值=并发槽位排队饥饿（健康、下轮就跑），绝不误杀
    # （守"慢≠故障"）。只有真被依赖卡死的才软掉账。
    from swarm.brain.nodes.dispatch import _soft_drop_aged_promises
    plan = TaskPlan(subtasks=[_mk("st-ready"), _mk("st-done-dep")])
    # st-ready 依赖 st-done-dep，但 st-done-dep 已完成 → st-ready 无未满足依赖 = 仅排队等槽位。
    plan.subtasks[0].depends_on = ["st-done-dep"]
    ages = {"st-ready": 99}   # 账龄极高但依赖已满足
    new_dr, new_ab, _, aged = _soft_drop_aged_promises(
        ages, ["st-ready"], [], plan, {"st-done-dep"}, 24)
    assert aged == [] and new_ab == [], "并发饥饿的健康排队项被误杀（#109 过激）"


def test_109_below_hard_threshold_no_drop():
    from swarm.brain.nodes.dispatch import _soft_drop_aged_promises
    plan = TaskPlan(subtasks=[_mk("st-up"), _mk("st-mid", depends=["st-up"])])
    ages = {"st-mid": 23}   # < 24 硬阈值
    new_dr, new_ab, _, aged = _soft_drop_aged_promises(ages, ["st-up", "st-mid"], [], plan, set(), 24)
    assert aged == [] and new_ab == [] and new_dr == ["st-up", "st-mid"], "未到硬阈值不得掉账"


# ── #108 执行期签名keyed 不收敛熔断（survives ID 增殖）─────────────────────────────
def test_108_sig_fuse_fires_at_k():
    from swarm.brain.nodes.failure import _exec_fail_sig_fuse, _normalize_fail_sig
    dfr = "build_fail: cannot find symbol foo at line 42"
    sig = _normalize_fail_sig(dfr)
    res = {"st-32-1-1": {"l1_details": {"det_fail_reason": dfr}}}
    counts, fused = _exec_fail_sig_fuse(["st-32-1-1"], res, {sig: 5}, 6)
    assert fused == ["st-32-1-1"], fused
    assert counts[sig] == 6


def test_108_survives_id_proliferation_and_line_jitter():
    # round66 死型治本点：ID 增殖（st-32→st-32-1→…）+ 行号抖动，per-id 计数器全被架空；
    # 归一签名跨 id 累计 → 第 6 个不同 id 触发熔断。
    from swarm.brain.nodes.failure import _exec_fail_sig_fuse
    c, fused = {}, []
    for i, sid in enumerate(
            ["st-32", "st-32-1", "st-32-1-1", "st-32-1-1-1", "st-32-1-1-1-2", "st-x"]):
        res = {sid: {"l1_details": {"det_fail_reason": f"build_fail: bad thing at line {i}"}}}
        c, fused = _exec_fail_sig_fuse([sid], res, c, 6)
    assert fused == ["st-x"], f"6 个不同 id 同归一签名应在第 6 个触发: {fused}"


def test_108_blocked_and_infra_not_counted():
    # blocked=sibling-wait / 纯 infra（无 det_fail_reason）不入账——重试是其正确语义，防误杀合法等待。
    from swarm.brain.nodes.failure import _exec_fail_sig_fuse
    res = {"a": {"l1_details": {"pipeline_blocked": "internal_pkg_not_built",
                                "det_fail_reason": "build_fail: x"}},
           "b": {"l1_details": {}}}
    counts, fused = _exec_fail_sig_fuse(["a", "b"], res, {}, 6)
    assert fused == [] and counts == {}, "blocked/infra 不应入账"


def test_108_wrapper_chokepoint_persists_counts_endtoend():
    # ★复核 CONFIRMED CRITICAL 整改端到端验证★：exec_fail_sig_counts 必须由 handle_failure wrapper
    # 唯一咽喉回写——无论 impl 走哪条 return（默认重试阶梯/replan/redecompose 等），结果都必须带上此键，
    # 且跨轮累积。旧实现只在 5 处窄 return 手写→ID 增殖路径漏写→熔断名存实亡。
    from swarm.brain.nodes import handle_failure
    st = SubTask(id="st-a", description="d",
                 scope=FileScope(create_files=["m/src/main/java/A.java"]),
                 harness=TaskHarness(language="java"))
    dfr = "build_fail: cannot find symbol Foo"
    _res = {"st-a": WorkerOutput(subtask_id="st-a", diff="+x", summary="", l1_passed=False,
                                 l1_details={"det_fail_reason": dfr, "l1_2_compile_ok": False})}
    state = {"plan": TaskPlan(subtasks=[st]), "failed_subtask_ids": ["st-a"],
             "subtask_results": _res, "dispatch_remaining": [],
             "subtask_retry_counts": {"st-a": 99},  # 逼确定性终局路径（避免依赖 LLM 策略）
             "exec_fail_sig_counts": {}}
    r = asyncio.run(handle_failure(state))
    assert "exec_fail_sig_counts" in r, "wrapper 咽喉未回写签名账（熔断名存实亡）"
    assert sum(r["exec_fail_sig_counts"].values()) >= 1, r["exec_fail_sig_counts"]
    # 跨轮累积：结果喂回 prior 再调一次，同签名计数增长
    state2 = {**state, "exec_fail_sig_counts": r["exec_fail_sig_counts"], "subtask_results": _res}
    r2 = asyncio.run(handle_failure(state2))
    assert max(r2["exec_fail_sig_counts"].values()) >= 2, f"跨轮未累积: {r2['exec_fail_sig_counts']}"
