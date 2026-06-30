#!/usr/bin/env python3
"""结构主干 A / B 治本不变量回归测试。

主干A（并行子任务共享聚合态）：worker 的 diff 必须是 (HEAD, 本 worker 自己 pull-back 的产出)
  的纯函数——即使【共享工作树】被另一个并发 worker 的 pull-back 覆盖（last-write-wins），
  本 worker 的 diff 仍反映自己的 +<module>，不被别人污染。
主干B（工作单元 vs 执行预算）：
  ①DISPATCH 预算闸门——超文件上界的工作单元派发前确定性拆小，不让大块进 worker（预防）。
  ②超时→强制拆小作【第一恢复动作】——coding/locating 超时先确定性拆小，而非先换模型重试同样
    的大块（round10 实证磨到取消）；不可拆/preparing 超时交常规阶梯。
"""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

from swarm.brain.nodes import (
    _is_timeout_oversize_failure,
    _redecompose_timeout_subtasks,
    handle_failure,
)
from swarm.brain.nodes.dispatch import _enforce_dispatch_budget_gate
from swarm.brain.planning_nodes import _oversized_by_files
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


def _st(sid, writable, depends_on=None):
    return SubTask(id=sid, description=f"建 {sid}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable), depends_on=depends_on or [])


# 7 文件单实体（全 java core）：anchors 单 Controller → 触发 round10 单实体按层拆
# （logic=Controller/Service/ServiceImpl，data=其余），确定性产出 ≥2 块。用于"可拆"夹具。
_ENTITY7 = [
    "com/x/Alarm.java", "com/x/AlarmMapper.java", "com/x/AlarmVO.java",
    "com/x/AlarmDTO.java", "com/x/AlarmController.java", "com/x/AlarmService.java",
    "com/x/AlarmServiceImpl.java",
]


def _stc(sid, creates, depends_on=None):
    """create_files 型子任务（_split_oversized_by_files 按 create 文件的实体/层拆分）。"""
    return SubTask(id=sid, description=f"建 {sid}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=creates), depends_on=depends_on or [])


def _wo(sid, ok=True):
    return WorkerOutput(subtask_id=sid, diff="d" if ok else "", summary="", l1_passed=ok,
                        l1_details={}, confidence="high" if ok else "low")


def _wo_timeout(sid, marker="timeout_in_coding"):
    return WorkerOutput(subtask_id=sid, diff="", summary="超时", l1_passed=False,
                        l1_details={"error": marker}, confidence="low")


def _run(coro):
    return asyncio.run(coro)


# ──────────────────────── 主干B ② 超时分类 ────────────────────────

def test_timeout_classification():
    assert _is_timeout_oversize_failure(_wo_timeout("x", "timeout_in_coding")) is True
    assert _is_timeout_oversize_failure(_wo_timeout("x", "timeout_in_locating")) is True
    # preparing = 沙箱基础设施超时，非尺寸问题 → 不算（交常规/瞬时阶梯）
    assert _is_timeout_oversize_failure(_wo_timeout("x", "timeout_in_preparing")) is False
    # 普通能力失败 → 不算
    assert _is_timeout_oversize_failure(_wo("x", ok=False)) is False
    assert _is_timeout_oversize_failure(None) is False


# ──────────────────────── 主干B ② 超时强制拆小 ────────────────────────

def test_timeout_multifile_force_split():
    """多文件超时（7 文件单实体）→ 一次性拆小重派、保留成功兄弟、出完成态。"""
    X = _stc("st-2", _ENTITY7)
    plan = TaskPlan(subtasks=[_st("st-1", ["s1.java"]), X])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _wo("st-1"), "st-2": _wo_timeout("st-2")},
        "dispatch_remaining": [],
        "subtask_redecompose_count": {},
    }
    out = _run(_redecompose_timeout_subtasks(state, ["st-2"]))
    assert out is not None
    assert out["failure_strategy"] == "retry"
    ids = [s.id for s in out["plan"].subtasks]
    assert "st-2" not in ids and any(i.startswith("st-2-") for i in ids)
    assert "st-1" in out["subtask_results"], "成功兄弟必须保留"
    assert "st-2" not in out["subtask_results"], "超时块出完成态待重做"
    assert out["failed_subtask_ids"] == [], "全部可拆 → 无 leftover"
    assert out["subtask_redecompose_count"]["st-2"] == 1


def test_timeout_single_file_not_split_falls_through():
    """单/双文件超时拆不动 → 返回 None（交常规阶梯换模型/升级）。"""
    X = _st("st-2", ["only.java"])
    plan = TaskPlan(subtasks=[_st("st-1", ["s1.java"]), X])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _wo("st-1"), "st-2": _wo_timeout("st-2")},
        "dispatch_remaining": [],
        "subtask_redecompose_count": {},
    }
    out = _run(_redecompose_timeout_subtasks(state, ["st-2"]))
    assert out is None


def test_timeout_already_split_not_resplit():
    """已拆过 1 次的超时块 → 不再拆（熔断），返回 None。"""
    X = _stc("st-2", _ENTITY7)
    plan = TaskPlan(subtasks=[X])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-2": _wo_timeout("st-2")},
        "dispatch_remaining": [],
        "subtask_redecompose_count": {"st-2": 1},
    }
    out = _run(_redecompose_timeout_subtasks(state, ["st-2"]))
    assert out is None


def test_timeout_mixed_batch_preserves_leftover():
    """一批里 1 个可拆超时 + 1 个不可拆超时：拆可拆的，不可拆的留在 failed 不被静默吞掉。"""
    big = _stc("st-2", _ENTITY7)
    tiny = _st("st-3", ["only.java"])
    plan = TaskPlan(subtasks=[big, tiny])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-2", "st-3"],
        "subtask_results": {"st-2": _wo_timeout("st-2"), "st-3": _wo_timeout("st-3")},
        "dispatch_remaining": [],
        "subtask_redecompose_count": {},
    }
    out = _run(_redecompose_timeout_subtasks(state, ["st-2", "st-3"]))
    assert out is not None
    # st-3 拆不动 → 必须留在 failed_subtask_ids（否则被 completed_ids 当成已完成静默漏到 MERGE）
    assert "st-3" in out["failed_subtask_ids"]
    assert "st-3" in out["subtask_results"], "未拆失败块的结果不可丢"
    ids = [s.id for s in out["plan"].subtasks]
    assert "st-2" not in ids and any(i.startswith("st-2-") for i in ids)


def test_handle_failure_timeout_preempts_before_llm():
    """超时在 handle_failure 里先于 LLM 故障分析被强制拆小（LLM 一旦被调用即判失败）。"""
    X = _stc("st-2", _ENTITY7)
    plan = TaskPlan(subtasks=[_st("st-1", ["s1.java"]), X])
    state = {
        "plan": plan,
        "complexity": Complexity.MEDIUM,  # 避开 SIMPLE 快路径
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _wo("st-1"), "st-2": _wo_timeout("st-2")},
        "dispatch_remaining": [],
        "subtask_redecompose_count": {},
    }

    def _boom(*a, **k):
        raise AssertionError("LLM 不应被调用——超时应在 LLM 故障分析前被强制拆小")

    with patch("swarm.brain.nodes._get_brain_llm", _boom):
        out = _run(handle_failure(state))
    assert out["failure_strategy"] == "retry"
    ids = [s.id for s in out["plan"].subtasks]
    assert "st-2" not in ids and any(i.startswith("st-2-") for i in ids)
    assert "st-1" in out["subtask_results"]


# ──────────────────────── 主干B ① DISPATCH 预算闸门 ────────────────────────

def test_dispatch_gate_splits_oversized_before_dispatch():
    """超文件上界（5>4）子任务派发前确定性拆小：plan 重建、子块入 remaining、收敛不再超界。"""
    big = _stc("st-2", _ENTITY7)
    small = _st("st-1", ["s.java"])
    plan = TaskPlan(subtasks=[small, big])
    new_plan, remaining, to_dispatch = _enforce_dispatch_budget_gate(
        plan, set(), ["st-1", "st-2"], 4, [big]
    )
    assert new_plan is not plan, "应重建 plan"
    ids = [s.id for s in new_plan.subtasks]
    assert "st-2" not in ids, "大块不再进 worker"
    assert len([i for i in ids if i.startswith("st-2-")]) >= 2
    assert "st-2" not in remaining
    # 收敛：拆出的子块都不再超文件上界（幂等，下轮直接放行）
    for s in new_plan.subtasks:
        assert not _oversized_by_files(s)


def test_dispatch_gate_noop_when_within_budget():
    """全在预算内 → 闸门不动 plan（幂等、零开销）。"""
    small = _st("st-1", ["s.java"])
    plan = TaskPlan(subtasks=[small])
    new_plan, remaining, to_dispatch = _enforce_dispatch_budget_gate(
        plan, set(), ["st-1"], 4, [small]
    )
    assert new_plan is plan, "未超界不应重建 plan"
    assert remaining == ["st-1"]


# ──────────────────────── 主干A：diff 不被并发 worker 污染 ────────────────────────

_POM_BASE = (
    "<project>\n"
    "  <modules>\n"
    "    <module>moduleA</module>\n"
    "  </modules>\n"
    "</project>\n"
)
_POM_OWN = (
    "<project>\n"
    "  <modules>\n"
    "    <module>moduleA</module>\n"
    "    <module>moduleB</module>\n"
    "  </modules>\n"
    "</project>\n"
)
_POM_CORRUPT = (
    "<project>\n"
    "  <modules>\n"
    "    <module>moduleA</module>\n"
    "    <module>moduleC</module>\n"
    "  </modules>\n"
    "</project>\n"
)


def test_trunkA_diff_reflects_own_output_not_corrupted_tree():
    """共享工作树被另一并发 worker 覆盖（+moduleC）后，本 worker 的 diff 仍是自己的 +moduleB。

    没有主干A 修复时：diff 取"工作区当前内容"=被覆盖的 +moduleC → 本 worker 的 +moduleB 丢失。
    有修复：锁内先用本 worker 的 _post_sync_contents 重置自产出，再 diff → 必为 +moduleB、无 C。
    """
    from swarm.worker.executor import WorkerExecutor

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        import os
        run_env = {**os.environ, **env}
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
        (root / "pom.xml").write_text(_POM_BASE, encoding="utf-8")
        subprocess.run(["git", "add", "pom.xml"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, env=run_env, check=True)
        # 模拟另一并发 worker 的 pull-back 把共享工作树 last-write-wins 覆盖成 +moduleC
        (root / "pom.xml").write_text(_POM_CORRUPT, encoding="utf-8")

        stub = types.SimpleNamespace(
            project_path=str(root),
            effective_scope=types.SimpleNamespace(
                writable=["pom.xml"], create_files=[], delete_files=[]),
            _repaired_extra_paths=set(),
            _post_sync_contents={"pom.xml": _POM_OWN},  # 本 worker 自己的产出 = +moduleB
            _sandbox_manager=None,
            _log=lambda *a, **k: None,
        )
        diff = WorkerExecutor._try_local_git_diff(stub)

    assert diff is not None and diff != "(无变更)"
    assert "moduleB" in diff, "本 worker 自己的 +moduleB 必须出现在 diff 中"
    assert "moduleC" not in diff, "另一并发 worker 覆盖的 +moduleC 不得污染本 worker 的 diff"


if __name__ == "__main__":
    import sys
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v()
                print(f"PASS {k}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {k}: {e}")
    sys.exit(1 if fails else 0)
