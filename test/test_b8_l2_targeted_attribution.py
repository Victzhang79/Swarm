"""TD2606-B8 回归：L2 集成失败定向归因 + 保留成功兄弟，杜绝单点集成问题连坐全量 replan。

背景：原 _l2_failure_state 把 subtask_results 全部 key 标失败 → handle_failure 全量 replan，
40/41 个 L1 通过的子任务被推倒重来。修复：据 integration_review 编译输出把失败定位到具体
子任务（写权文件出现在编译错误里），只重做相关子任务、保留成功兄弟；定位不了才回退全量 replan。
"""
import asyncio

from swarm.brain.nodes import handle_failure
from swarm.brain.nodes.shared import attribute_l2_failure, build_writers_by_file
from swarm.brain.nodes.verify import _l2_failure_state
from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput


def _st(sid, *, writable=None, create=None):
    return SubTask(
        id=sid,
        description=f"subtask {sid}",
        scope=FileScope(writable=writable or [], create_files=create or []),
    )


def _plan(*subtasks):
    return TaskPlan(subtasks=list(subtasks))


def _wo(sid, l1_passed=True):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if l1_passed else "",
        summary="",
        l1_passed=l1_passed,
        l1_details={},
        confidence="high" if l1_passed else "low",
    )


# ── build_writers_by_file ──

def test_build_writers_by_file_maps_create_and_writable():
    plan = _plan(
        _st("st-1", create=["src/A.java"]),
        _st("st-2", writable=["src/B.java"], create=["src/C.java"]),
    )
    writers = build_writers_by_file(plan)
    assert writers["src/A.java"] == ["st-1"]
    assert writers["src/B.java"] == ["st-2"]
    assert writers["src/C.java"] == ["st-2"]


# ── attribute_l2_failure ──

def test_attribute_localizes_single_failing_subtask():
    plan = _plan(
        _st("st-1", create=["src/A.java"]),
        _st("st-2", create=["src/B.java"]),
        _st("st-3", create=["src/C.java"]),
    )
    results = {"st-1": _wo("st-1"), "st-2": _wo("st-2"), "st-3": _wo("st-3")}
    details = {
        "integration_review": {
            "compile_output": "[ERROR] /workspace/src/B.java:[12,5] cannot find symbol\n"
        },
        "issues": ["L2.1 compile failed: src/B.java:[12,5] cannot find symbol"],
    }
    assert attribute_l2_failure(plan, details, results) == ["st-2"]


def test_attribute_returns_none_when_no_file_matches():
    plan = _plan(_st("st-1", create=["src/A.java"]), _st("st-2", create=["src/B.java"]))
    results = {"st-1": _wo("st-1"), "st-2": _wo("st-2")}
    details = {"integration_review": {"compile_output": "BUILD FAILURE: generic error"}}
    assert attribute_l2_failure(plan, details, results) is None


def test_attribute_returns_none_when_all_subtasks_match():
    # 全部子任务都命中 → 非真子集 → 回退全量 replan（不误判为"只重做一部分"）
    plan = _plan(_st("st-1", create=["src/A.java"]), _st("st-2", create=["src/B.java"]))
    results = {"st-1": _wo("st-1"), "st-2": _wo("st-2")}
    details = {"integration_review": {"compile_output": "err src/A.java and src/B.java"}}
    assert attribute_l2_failure(plan, details, results) is None


def test_attribute_returns_none_without_evidence():
    plan = _plan(_st("st-1", create=["src/A.java"]))
    assert attribute_l2_failure(plan, {}, {"st-1": _wo("st-1")}) is None


# ── _l2_failure_state ──

def test_l2_failure_state_targeted_flag():
    targeted = _l2_failure_state({"a": 1, "b": 2}, attributed_ids=["b"], l2_details={"x": 1})
    assert targeted["l2_targeted"] is True
    assert targeted["failed_subtask_ids"] == ["b"]
    assert targeted["l2_details"] == {"x": 1}


def test_l2_failure_state_blanket_when_no_attribution():
    blanket = _l2_failure_state({"a": 1, "b": 2})
    assert "l2_targeted" not in blanket
    assert set(blanket["failed_subtask_ids"]) == {"a", "b"}


# ── handle_failure：L2 定向恢复 vs 全量 replan ──

def _run(state):
    return asyncio.run(handle_failure(state))


def test_handle_failure_l2_targeted_preserves_siblings():
    state = {
        "verification_failure": "l2",
        "l2_targeted": True,
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _wo("st-1", True), "st-2": _wo("st-2", True)},
        "dispatch_remaining": [],
        "subtask_retry_counts": {},
        "replan_count": 0,
    }
    result = _run(state)
    assert result["failure_strategy"] == "retry"
    assert "st-1" in result["subtask_results"], "成功兄弟 st-1 不可被清空"
    assert "st-2" not in result["subtask_results"], "归因到的 st-2 应被移除待重做"
    assert "st-2" in result["dispatch_remaining"]
    assert result["replan_count"] == 1, "定向恢复仍自增 replan_count（共用熔断）"
    assert result["targeted_recovery"] is True


def test_handle_failure_l2_blanket_replan_when_not_targeted():
    state = {
        "verification_failure": "l2",
        "failed_subtask_ids": ["st-1", "st-2"],
        "subtask_results": {"st-1": _wo("st-1", True), "st-2": _wo("st-2", False)},
        "dispatch_remaining": [],
        "replan_count": 0,
    }
    result = _run(state)
    assert result["failure_strategy"] == "replan"
    assert result["failed_subtask_ids"] == []


def test_handle_failure_l2_targeted_escalates_at_circuit_limit():
    # replan_count 已达上限 → 即便 targeted 也升级人工（熔断优先）
    state = {
        "verification_failure": "l2",
        "l2_targeted": True,
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _wo("st-1", True), "st-2": _wo("st-2", True)},
        "dispatch_remaining": [],
        "replan_count": 99,
    }
    result = _run(state)
    assert result["failure_strategy"] == "escalate"
