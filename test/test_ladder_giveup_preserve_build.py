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


# ── 治本 replan 死循环：helper 单元 ───────────────────────────────────────
def test_transitive_abandon_closure():
    subs = [_st("a"), _st("b", depends_on=["a"]), _st("c", depends_on=["b"]), _st("d")]
    assert nodes._transitive_abandon(subs, {"a"}) == {"a", "b", "c"}
    assert nodes._transitive_abandon(subs, {"d"}) == {"d"}
    assert nodes._transitive_abandon(subs, set()) == set()


def test_producers_of_module_and_package():
    plan = TaskPlan(subtasks=[
        _st("st-up", writable=["ruoyi-alarm-sdk/src/main/java/com/ruoyi/alarm/sdk/client/HttpClientUtils.java"]),
        _st("st-other", writable=["ruoyi-common/src/main/java/com/ruoyi/common/X.java"]),
    ])
    assert nodes._producers_of(plan, [], ["ruoyi-alarm-sdk"]) == {"st-up"}            # 模块归属
    assert nodes._producers_of(plan, ["com.ruoyi.alarm.sdk.client"], []) == {"st-up"}  # 包归属
    assert nodes._producers_of(plan, ["com.nope"], ["nope-mod"]) == set()             # 无关


# ── 治本 replan 死循环核心：上游∈放弃集的下游 BLOCKED → 直接连坐放弃，不 replan ──
def test_handle_failure_downstream_of_abandoned_upstream_abandons_not_replan():
    """round12 真因：st-up 被阶梯三放弃后，下游 st-down 永久 upstream_module_broken。
    旧行为 LLM→replan→守卫降级 retry→重派→BLOCKED 无界循环；新行为直接传递放弃→PARTIAL。"""
    plan = TaskPlan(subtasks=[
        _st("st-up", writable=["modA/Up.java"]),
        # st-down 跨模块 import modA（plan 期拿不到，depends_on 为空）→ 必须靠 runtime blocked_on 映射
        _st("st-down", writable=["modB/Down.java"]),
        _st("st-tail", writable=["modC/Tail.java"], depends_on=["st-down"]),  # 传递下游
    ])
    down_out = WorkerOutput(
        subtask_id="st-down", diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "upstream_module_broken", "blocked_on_modules": ["modA"]},
    )

    class _ReplanLLM:  # 若 B 没短路，LLM 会让它 replan（断言 abandon 即证 B 先于 LLM 生效）
        async def ainvoke(self, _m):
            class _R:
                content = '{"strategy":"replan","reasoning":"x"}'
            return _R()

    state = {
        "plan": plan, "project_id": "p1",
        "failed_subtask_ids": ["st-down"],
        "subtask_results": {"st-up": _wo("st-up"), "st-down": down_out},
        "give_up_isolated_ids": ["st-up"], "abandoned_subtask_ids": [],
        "dispatch_remaining": ["st-down", "st-tail"],
    }
    with patch.object(nodes, "_get_brain_llm", lambda: _ReplanLLM()):
        out = _run(nodes.handle_failure(state))
    assert out["failure_strategy"] == "abandon", out.get("failure_strategy")
    # st-down(blocked on 放弃模块) + st-tail(传递依赖) 一并放弃；不再 retry/replan
    assert set(out["abandoned_subtask_ids"]) >= {"st-down", "st-tail"}
    assert out["failed_subtask_ids"] == []
    assert "st-down" not in out["subtask_results"]


def test_handle_failure_downstream_via_depends_on_also_short_circuits():
    """depends_on 显式声明命中放弃集（口径2）→ 同样直接放弃。"""
    plan = TaskPlan(subtasks=[
        _st("st-up", writable=["modA/Up.java"]),
        _st("st-down", writable=["modB/Down.java"], depends_on=["st-up"]),
    ])
    down_out = WorkerOutput(
        subtask_id="st-down", diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "internal_pkg_not_built", "blocked_on_packages": []},
    )

    class _ReplanLLM:
        async def ainvoke(self, _m):
            class _R:
                content = '{"strategy":"replan"}'
            return _R()

    state = {
        "plan": plan, "project_id": "p1",
        "failed_subtask_ids": ["st-down"],
        "subtask_results": {"st-up": _wo("st-up"), "st-down": down_out},
        "give_up_isolated_ids": [], "abandoned_subtask_ids": ["st-up"],
        "dispatch_remaining": ["st-down"],
    }
    with patch.object(nodes, "_get_brain_llm", lambda: _ReplanLLM()):
        out = _run(nodes.handle_failure(state))
    assert out["failure_strategy"] == "abandon"
    assert "st-down" in out["abandoned_subtask_ids"]


def test_handle_failure_blocked_but_upstream_not_abandoned_does_not_short_circuit():
    """上游未被放弃(仍在重试中)→不短路：BLOCKED 走正常 transient 退避，等上游真落地。"""
    plan = TaskPlan(subtasks=[
        _st("st-up", writable=["modA/Up.java"]),
        _st("st-down", writable=["modB/Down.java"]),
    ])
    down_out = WorkerOutput(
        subtask_id="st-down", diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "upstream_module_broken", "blocked_on_modules": ["modA"],
                    "failure_class": "transient"},
    )
    state = {
        "plan": plan, "project_id": "p1",
        "failed_subtask_ids": ["st-down"],
        "subtask_results": {"st-down": down_out},
        "give_up_isolated_ids": [], "abandoned_subtask_ids": [],  # 上游未放弃
        "dispatch_remaining": ["st-down"], "subtask_transient_counts": {},
    }
    out = _run(nodes.handle_failure(state))
    assert out.get("failure_strategy") != "abandon", "上游未放弃不应短路放弃下游"


# ── #R13-2：臆造不存在的包(无生产者+基线不存在) → 硬失败连坐，不空烧 transient 阶梯 ──
def _blocked_pkg_state(pkg):
    """单个失败子任务 BLOCKED on 某包，无任何子任务生产该包；无预放弃集。"""
    plan = TaskPlan(subtasks=[_st("st-solo", writable=["modB/Down.java"])])
    out = WorkerOutput(
        subtask_id="st-solo", diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "internal_pkg_not_built",
                    "blocked_on_packages": [pkg], "failure_class": "transient"},
    )
    return {
        "plan": plan, "project_id": "p1",
        "failed_subtask_ids": ["st-solo"],
        "subtask_results": {"st-solo": out},
        "give_up_isolated_ids": [], "abandoned_subtask_ids": [],
        "dispatch_remaining": ["st-solo"], "subtask_transient_counts": {},
    }


def test_handle_failure_hallucinated_pkg_no_producer_not_in_baseline_abandons():
    """臆造包：无 plan 生产者 且 基线树无此包 → 判不可恢复、连坐放弃(不再 transient 空烧)。"""
    state = _blocked_pkg_state("com.ruoyi.common.core.redis")
    with patch.object(nodes, "_package_in_baseline", return_value=False):
        out = _run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "abandon", "臆造不存在的包应硬失败连坐放弃"
    assert "st-solo" in (out.get("abandoned_subtask_ids") or [])


def test_handle_failure_blocked_pkg_in_baseline_does_not_abandon():
    """假阳性护栏：包【在基线树里】(仅沙箱漏同步) → 不判臆造，继续 transient 等待、不放弃。"""
    state = _blocked_pkg_state("com.ruoyi.common.utils")
    with patch.object(nodes, "_package_in_baseline", return_value=True):
        out = _run(nodes.handle_failure(state))
    assert out.get("failure_strategy") != "abandon", "基线已有的包不可硬失败(可能只是沙箱漏同步)"


def test_handle_failure_blocked_pkg_has_producer_does_not_abandon():
    """假阳性护栏：包由某【未放弃的】子任务生产 → 不判臆造，等它落地、不放弃。"""
    plan = TaskPlan(subtasks=[
        _st("st-solo", writable=["modB/Down.java"]),
        _st("st-prod", writable=["modC/src/main/java/com/real/svc/Svc.java"]),
    ])
    out = WorkerOutput(
        subtask_id="st-solo", diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "internal_pkg_not_built",
                    "blocked_on_packages": ["com.real.svc"], "failure_class": "transient"},
    )
    state = {
        "plan": plan, "project_id": "p1", "failed_subtask_ids": ["st-solo"],
        "subtask_results": {"st-solo": out}, "give_up_isolated_ids": [],
        "abandoned_subtask_ids": [], "dispatch_remaining": ["st-solo"],
        "subtask_transient_counts": {},
    }
    # 即便基线无此包，只要有【未放弃的生产者】就不判臆造(等生产者落地)
    with patch.object(nodes, "_package_in_baseline", return_value=False):
        out2 = _run(nodes.handle_failure(state))
    assert out2.get("failure_strategy") != "abandon", "有未放弃生产者的包不可判臆造"


def test_package_in_baseline_detects_present_and_absent(tmp_path):
    """_package_in_baseline 纯函数：存在的包→True，不存在→False，无路径→保守 True。"""
    (tmp_path / "ruoyi-common/src/main/java/com/ruoyi/common/utils").mkdir(parents=True)
    assert nodes._package_in_baseline(str(tmp_path), "com.ruoyi.common.utils") is True
    assert nodes._package_in_baseline(str(tmp_path), "com.ruoyi.common.core.redis") is False
    assert nodes._package_in_baseline(None, "com.x") is True  # 无路径→保守当存在，不误杀
    assert nodes._package_in_baseline(str(tmp_path), "") is True


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
