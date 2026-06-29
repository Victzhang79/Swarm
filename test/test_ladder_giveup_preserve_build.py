#!/usr/bin/env python3
"""卡死子任务恢复阶梯·阶梯三：保 build 放弃（revert / 可编译桩 / 自动判依赖）。

阶梯一(retry)+阶梯二(拆小)耗尽仍失败、有成功兄弟 → 不再直接 escalate 全盘 FAILED，而是：
  - 不被依赖 → revert：清【本地树足迹】(防 -am reactor 中毒)，只丢 X，零连坐；
  - 被依赖 → 可编译桩：救下游编译，桩失败回退 revert + 传递放弃下游；
两路都给 X 终态计入 completed、记 give_up_isolated_ids，run 继续 merge→L2，终态 PARTIAL 诚实交付。
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.brain.nodes import (
    _give_up_preserve_build,
    _local_tree_revert_subtask,
    _subtask_footprint,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput


def _st(sid, writable=None, create_files=None, depends_on=None):
    return SubTask(id=sid, description=f"建 {sid}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create_files or []),
                   depends_on=depends_on or [])


def _wo(sid, ok=True):
    return WorkerOutput(subtask_id=sid, diff="d" if ok else "", summary="", l1_passed=ok)


def _run(coro):
    return asyncio.run(coro)


def _async_return(val):
    async def _f(*a, **k):
        return val
    return _f


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


# ── _local_tree_revert_subtask：tracked→checkout、untracked→rm ──────────
def test_revert_removes_untracked_and_restores_tracked(tmp_path):
    repo = _git_repo(tmp_path)
    # 已跟踪文件：提交干净版，再写脏内容 → revert 应还原为提交版
    tracked = repo / "Keep.java"
    tracked.write_text("ORIG", encoding="utf-8")
    subprocess.run(["git", "add", "Keep.java"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    tracked.write_text("DIRTY", encoding="utf-8")
    # 未跟踪新建文件：revert 应删除
    created = repo / "sub" / "New.java"
    created.parent.mkdir(parents=True)
    created.write_text("BROKEN", encoding="utf-8")

    st = _st("st-x", writable=["Keep.java"], create_files=["sub/New.java"])
    res = _local_tree_revert_subtask(str(repo), st)

    assert tracked.read_text() == "ORIG", "已跟踪脏文件应被还原为 HEAD 版"
    assert not created.exists(), "未跟踪新建文件应被删除"
    assert "Keep.java" in res["reverted"]
    assert "sub/New.java" in res["removed"]


def test_revert_noop_without_git(tmp_path):
    st = _st("st-x", create_files=["a.java"])
    res = _local_tree_revert_subtask(str(tmp_path), st)  # 非 git 仓库
    assert res == {"reverted": [], "removed": []}


def test_subtask_footprint_union_dedup():
    st = _st("x", writable=["a.java", "/b.java"], create_files=["a.java", "c.java"])
    assert _subtask_footprint(st) == ["a.java", "b.java", "c.java"]


# ── 编排：不被依赖 → revert，只丢 X，保留兄弟 ───────────────────────────
def test_giveup_not_depended_reverts_only_x(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "X.java").write_text("BROKEN", encoding="utf-8")
    plan = TaskPlan(subtasks=[_st("st-1", writable=["s1.java"]),
                              _st("st-x", create_files=["X.java"])])
    state = {
        "plan": plan,
        "project_id": "p1",
        "subtask_results": {"st-1": _wo("st-1"), "st-x": _wo("st-x", ok=False)},
        "dispatch_remaining": [],
        "give_up_isolated_ids": [],
        "abandoned_subtask_ids": [],
    }
    with patch.object(nodes, "_proj_path_from_state", return_value=str(repo)):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    assert out is not None
    assert out["failure_strategy"] == "give_up_preserve"
    assert "st-x" in out["give_up_isolated_ids"]
    assert out["abandoned_subtask_ids"] == [], "无人依赖 X → 零连坐"
    assert "st-1" in out["subtask_results"], "成功兄弟保留"
    xo = out["subtask_results"]["st-x"]
    assert xo.l1_passed is True and xo.diff == "" and xo.l1_details.get("give_up_mode") == "revert"
    assert not (repo / "X.java").exists(), "X 坏文件应从本地树清除（防 reactor 中毒）"


# ── 编排：被依赖 + 桩成功 → 下游不连坐放弃 ──────────────────────────────
def test_giveup_depended_stub_saves_dependents(tmp_path):
    plan = TaskPlan(subtasks=[
        _st("st-x", create_files=["X.java"]),
        _st("st-2", create_files=["Y.java"], depends_on=["st-x"]),
    ])
    state = {
        "plan": plan, "project_id": "p1",
        "subtask_results": {"st-x": _wo("st-x", ok=False), "st-2": _wo("st-2")},
        "dispatch_remaining": [], "give_up_isolated_ids": [], "abandoned_subtask_ids": [],
    }
    fake_diff = "diff --git a/X.java b/X.java\n+stub"
    with patch.object(nodes, "_proj_path_from_state", return_value="/tmp/fake"), \
         patch.object(nodes, "_generate_compile_stub", new=_async_return(fake_diff)):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    assert out["give_up_isolated_ids"] == ["st-x"]
    assert out["abandoned_subtask_ids"] == [], "桩成功 → 下游 st-2 不被连坐放弃"
    assert "st-2" in out["subtask_results"], "下游成果保留（编译靠桩）"
    xo = out["subtask_results"]["st-x"]
    assert xo.diff == fake_diff and xo.l1_details.get("give_up_mode") == "stub"


# ── 编排：被依赖 + 桩失败 → revert + 传递放弃下游 ───────────────────────
def test_giveup_depended_stub_fail_falls_back_revert_and_abandons_dependents(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "X.java").write_text("BROKEN", encoding="utf-8")
    plan = TaskPlan(subtasks=[
        _st("st-x", create_files=["X.java"]),
        _st("st-2", create_files=["Y.java"], depends_on=["st-x"]),
        _st("st-3", create_files=["Z.java"], depends_on=["st-2"]),  # 传递依赖
    ])
    state = {
        "plan": plan, "project_id": "p1",
        "subtask_results": {"st-x": _wo("st-x", ok=False), "st-2": _wo("st-2"), "st-3": _wo("st-3")},
        "dispatch_remaining": [], "give_up_isolated_ids": [], "abandoned_subtask_ids": [],
    }
    with patch.object(nodes, "_proj_path_from_state", return_value=str(repo)), \
         patch.object(nodes, "_generate_compile_stub", new=_async_return(None)):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    assert out["give_up_isolated_ids"] == ["st-x"]
    # 桩失败 revert → 下游 st-2 及传递依赖 st-3 缺依赖跑不了 → 连坐放弃
    assert set(out["abandoned_subtask_ids"]) == {"st-2", "st-3"}
    assert "st-2" not in out["subtask_results"] and "st-3" not in out["subtask_results"]
    assert not (repo / "X.java").exists()


# ── handle_failure 端到端：耗尽 + 单文件(阶梯二拆不动) → 阶梯三 give_up，非 escalate ──
def test_handle_failure_exhausted_single_file_giveup_not_escalate(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "Only.java").write_text("BROKEN", encoding="utf-8")
    plan = TaskPlan(subtasks=[_st("st-1", writable=["s1.java"]),
                              _st("st-x", create_files=["Only.java"])])  # 单文件 → 阶梯二跳过

    class _L:
        async def ainvoke(self, _m):
            class _R:
                content = '{"strategy":"replan","reasoning":"修不动"}'
            return _R()

    from swarm.config.settings import get_config
    cap = get_config().model.max_retries
    state = {
        "plan": plan, "project_id": "p1",
        "failed_subtask_ids": ["st-x"],
        "subtask_results": {"st-1": _wo("st-1"), "st-x": _wo("st-x", ok=False)},
        "subtask_retry_counts": {"st-x": cap + 2},  # 耗尽
        "dispatch_remaining": [], "give_up_isolated_ids": [], "abandoned_subtask_ids": [],
    }
    with patch.object(nodes, "_get_brain_llm", lambda: _L()), \
         patch.object(nodes, "_proj_path_from_state", return_value=str(repo)):
        out = _run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "give_up_preserve", out.get("failure_strategy")
    assert out.get("failure_escalated") is not True, "阶梯三消化 → 不再整任务 escalate FAILED"
    assert "st-x" in out.get("give_up_isolated_ids", [])
    assert "st-1" in out["subtask_results"], "成功兄弟保留"
    assert not (repo / "Only.java").exists(), "卡死 X 坏文件清出本地树"


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
