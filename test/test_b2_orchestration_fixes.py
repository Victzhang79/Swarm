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
