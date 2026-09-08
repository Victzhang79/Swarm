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
import swarm.brain.nodes.planning_core as pc
from swarm.brain.nodes import (
    _give_up_preserve_build,
    _local_tree_revert_subtask,
    _subtask_footprint,
)

# god-file 主线1：恢复阶梯簇已抽出到 planning_core。_give_up_preserve_build 内部对
# _proj_path_from_state / _generate_compile_stub 的【同簇互调】在 planning_core 命名空间解析，
# 故这些内部调用的 patch 目标须为 pc（patch nodes 命名空间只对 __init__ 内的调用点生效）。
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
    assert res == {"reverted": [], "removed": [], "revert_failed": [], "skipped_protected": []}


def test_revert_checkout_failure_not_marked_reverted(tmp_path):
    """E2 治本：git checkout rc!=0（未还原）绝不记 reverted——否则"放弃保 build"静默失效，
    脏改动仍毒 -am。用不存在的 base_ref 强制 checkout 失败，断言文件进 revert_failed 且内容未变。"""
    repo = _git_repo(tmp_path)
    tracked = repo / "Keep.java"
    tracked.write_text("ORIG", encoding="utf-8")
    subprocess.run(["git", "add", "Keep.java"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    tracked.write_text("DIRTY", encoding="utf-8")

    st = _st("st-x", writable=["Keep.java"])
    # base_ref = 不存在的 40-hex SHA → git checkout <bad> -- Keep.java 必 rc!=0（invalid reference）。
    res = _local_tree_revert_subtask(str(repo), st, base_ref="0" * 40)

    assert "Keep.java" not in res["reverted"], "checkout 失败绝不能记 reverted（假装已清）"
    assert "Keep.java" in res["revert_failed"], "checkout 失败应如实记入 revert_failed"
    assert tracked.read_text() == "DIRTY", "checkout 失败 → 文件确实未被还原（脏改动仍在）"


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
    with patch.object(pc, "_proj_path_from_state", return_value=str(repo)):
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
    # 32 号文 A10-M1：桩生成契约改为 (diff, written)——written=写盘事实单一事实源。
    with patch.object(pc, "_proj_path_from_state", return_value="/tmp/fake"), \
         patch.object(pc, "_generate_compile_stub",
                      new=_async_return((fake_diff, ["X.java"]))):
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
    with patch.object(pc, "_proj_path_from_state", return_value=str(repo)), \
         patch.object(pc, "_generate_compile_stub", new=_async_return(None)):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    assert out["give_up_isolated_ids"] == ["st-x"]
    # 桩失败 revert → 下游 st-2 及传递依赖 st-3 缺依赖跑不了 → 连坐放弃
    assert set(out["abandoned_subtask_ids"]) == {"st-2", "st-3"}
    assert "st-2" not in out["subtask_results"] and "st-3" not in out["subtask_results"]
    assert not (repo / "X.java").exists()


# ── R65C-T3：桩必须覆盖下游声明的 provenance（upstream_artifacts 完备性）────
def _stub_llm_returning(files: dict):
    import json as _json

    class _Resp:
        def __init__(self, c):
            self.content = c

    class _L:
        async def ainvoke(self, _msgs):
            return _Resp(_json.dumps({"files": files}, ensure_ascii=False))

    return lambda: _L()


def _plan_with_declared_downstream(x_files, declared):
    return TaskPlan(subtasks=[
        _st("st-x", create_files=list(x_files)),
        SubTask(id="st-d", description="下游", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=["m/src/B.java"],
                                upstream_artifacts=list(declared)),
                depends_on=["st-x"]),
    ])


def test_stub_missing_downstream_provenance_falls_back_revert(tmp_path):
    """R65C-T3（round65c 实锤面）：下游 upstream_artifacts 声明了上游的 pom，桩只
    产出代码文件 → 桩不完整（下游种子闸必 BLOCKED 永堵，#53 修①后还会反复撞闸
    烧失败预算）→ 必须回退 revert 诚实连坐，绝不 settled-with-product 假桩；
    不完整桩已写的文件必须清理，防半桩毒树。"""
    import swarm.brain.nodes as nodes
    repo = _git_repo(tmp_path)
    plan = _plan_with_declared_downstream(
        ["m/pom.xml", "m/src/A.java"], ["m/pom.xml", "m/src/A.java"])
    state = {"plan": plan, "project_id": "p1",
             "subtask_results": {"st-x": _wo("st-x", ok=False), "st-d": _wo("st-d")},
             "dispatch_remaining": [], "give_up_isolated_ids": [],
             "abandoned_subtask_ids": []}
    with patch.object(pc, "_proj_path_from_state", return_value=str(repo)), \
         patch.object(pc, "_git_diff_for_paths",
                      lambda *a, **k: "diff --git a/m/src/A.java b/m/src/A.java\n+stub"), \
         patch.object(nodes, "_get_brain_llm",
                      _stub_llm_returning({"m/src/A.java": "public class A {}"})):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    xo = out["subtask_results"]["st-x"]
    assert xo.l1_details.get("give_up_mode") == "revert", \
        "桩缺下游声明的 m/pom.xml → 不完整，必须回退 revert 而非假桩"
    assert "st-d" in out["abandoned_subtask_ids"], "revert 路下游诚实连坐"
    assert not (repo / "m" / "src" / "A.java").exists(), "不完整桩的已写文件必须清理"


def test_stub_accepts_downstream_declared_noncode_files(tmp_path):
    """R65C-T3：下游声明的非代码产物（构建清单等）必须允许打桩落树——_CODE_EXT
    过滤只适用于【未被下游声明】的文件。LLM 全覆盖时桩成立、pom 真实落树。"""
    import swarm.brain.nodes as nodes
    repo = _git_repo(tmp_path)
    plan = _plan_with_declared_downstream(
        ["m/pom.xml", "m/src/A.java"], ["m/pom.xml", "m/src/A.java"])
    state = {"plan": plan, "project_id": "p1",
             "subtask_results": {"st-x": _wo("st-x", ok=False), "st-d": _wo("st-d")},
             "dispatch_remaining": [], "give_up_isolated_ids": [],
             "abandoned_subtask_ids": []}
    with patch.object(pc, "_proj_path_from_state", return_value=str(repo)), \
         patch.object(pc, "_git_diff_for_paths",
                      lambda *a, **k: "diff --git a/m/pom.xml b/m/pom.xml\n+stub"), \
         patch.object(nodes, "_get_brain_llm", _stub_llm_returning({
             "m/src/A.java": "public class A {}",
             "m/pom.xml": "<project><modelVersion>4.0.0</modelVersion></project>",
         })):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    xo = out["subtask_results"]["st-x"]
    assert xo.l1_details.get("give_up_mode") == "stub", "provenance 全覆盖 → 桩成立"
    assert out["abandoned_subtask_ids"] == [], "桩完整 → 下游不连坐"
    assert (repo / "m" / "pom.xml").exists(), "下游声明的 pom 必须真实落树（种子闸依据）"


def test_stub_ignores_undeclared_noncode_files(tmp_path):
    """行为锁（两侧恒绿）：足迹里未被任何下游声明的非代码文件仍被过滤——
    「不乱碰构建文件」原则只对下游明确声明的 provenance 让路。"""
    import swarm.brain.nodes as nodes
    repo = _git_repo(tmp_path)
    plan = _plan_with_declared_downstream(
        ["m/pom.xml", "m/src/A.java"], ["m/src/A.java"])  # 下游只声明代码文件
    state = {"plan": plan, "project_id": "p1",
             "subtask_results": {"st-x": _wo("st-x", ok=False), "st-d": _wo("st-d")},
             "dispatch_remaining": [], "give_up_isolated_ids": [],
             "abandoned_subtask_ids": []}
    with patch.object(pc, "_proj_path_from_state", return_value=str(repo)), \
         patch.object(pc, "_git_diff_for_paths",
                      lambda *a, **k: "diff --git a/m/src/A.java b/m/src/A.java\n+stub"), \
         patch.object(nodes, "_get_brain_llm", _stub_llm_returning({
             "m/src/A.java": "public class A {}",
             "m/pom.xml": "<project/>",
         })):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    xo = out["subtask_results"]["st-x"]
    assert xo.l1_details.get("give_up_mode") == "stub"
    assert not (repo / "m" / "pom.xml").exists(), \
        "未被下游声明的构建文件绝不打桩（越权写过滤不回归）"


def test_required_set_uses_canonical_normalization(tmp_path):
    """猎手 R65C-T3 F2（CONFIRMED HIGH）：下游声明带 './' 前缀/反斜杠的路径口径漂移
    （R41 实证过的真实病）必须仍匹配上游足迹——否则 required 集静默算空，完备性闸
    退化为 no-op，round65c 死法零留痕复发。"""
    import swarm.brain.nodes as nodes
    repo = _git_repo(tmp_path)
    plan = _plan_with_declared_downstream(
        ["m/pom.xml", "m/src/A.java"], ["./m/pom.xml", "m\\src\\A.java"])
    state = {"plan": plan, "project_id": "p1",
             "subtask_results": {"st-x": _wo("st-x", ok=False), "st-d": _wo("st-d")},
             "dispatch_remaining": [], "give_up_isolated_ids": [],
             "abandoned_subtask_ids": []}
    with patch.object(pc, "_proj_path_from_state", return_value=str(repo)), \
         patch.object(pc, "_git_diff_for_paths",
                      lambda *a, **k: "diff --git a/m/src/A.java b/m/src/A.java\n+stub"), \
         patch.object(nodes, "_get_brain_llm",
                      _stub_llm_returning({"m/src/A.java": "public class A {}"})):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    xo = out["subtask_results"]["st-x"]
    assert xo.l1_details.get("give_up_mode") == "revert", \
        "口径漂移的声明必须归一后匹配足迹——桩缺 pom 应判不完整回退 revert"


def test_cascade_revert_failure_goes_to_degraded_reasons(tmp_path):
    """猎手 F2（CONFIRMED）：连坐放弃的下游 WorkerOutput 被 pop（无 l1_details 可挂账）
    ——其足迹清理失败必须走 degraded_reasons 机读留痕，绝不随 pop 无迹消失。"""
    repo = _git_repo(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-x", create_files=["X.java"]),
        _st("st-2", create_files=["Y.java"], depends_on=["st-x"]),
    ])
    state = {"plan": plan, "project_id": "p1",
             "subtask_results": {"st-x": _wo("st-x", ok=False), "st-2": _wo("st-2")},
             "dispatch_remaining": [], "give_up_isolated_ids": [],
             "abandoned_subtask_ids": []}

    def _dirty_only_downstream(project_path, st_, protected_files=None, base_ref=None):
        if getattr(st_, "id", "") == "st-2":
            return {"reverted": [], "removed": [], "revert_failed": ["Y.java"],
                    "skipped_protected": []}
        return {"reverted": [], "removed": ["X.java"], "revert_failed": [],
                "skipped_protected": []}

    with patch.object(pc, "_proj_path_from_state", return_value=str(repo)), \
         patch.object(pc, "_generate_compile_stub", new=_async_return(None)), \
         patch.object(pc, "_local_tree_revert_subtask", _dirty_only_downstream):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    assert "st-2" in out["abandoned_subtask_ids"]
    _dr = out.get("degraded_reasons") or []
    assert any(d.startswith("cascade_revert_failed:st-2") for d in _dr), \
        f"连坐下游清理失败必须入 degraded_reasons 机读账，得 {_dr}"


def test_revert_failure_surfaced_in_ledger(tmp_path):
    """猎手 R65C-T3 F1（CONFIRMED HIGH）：revert 清理失败（revert_failed 非空=树仍脏）
    绝不能账面写「已清」——必须机读留痕 l1_details.revert_failed，摘要如实说清理不完整。"""
    repo = _git_repo(tmp_path)
    plan = TaskPlan(subtasks=[_st("st-x", create_files=["X.java"])])
    state = {"plan": plan, "project_id": "p1",
             "subtask_results": {"st-x": _wo("st-x", ok=False)},
             "dispatch_remaining": [], "give_up_isolated_ids": [],
             "abandoned_subtask_ids": []}

    def _dirty_revert(project_path, st_, protected_files=None, base_ref=None):
        return {"reverted": [], "removed": [], "revert_failed": ["X.java"],
                "skipped_protected": []}

    with patch.object(pc, "_proj_path_from_state", return_value=str(repo)), \
         patch.object(pc, "_local_tree_revert_subtask", _dirty_revert):
        out = _run(_give_up_preserve_build(state, ["st-x"]))
    xo = out["subtask_results"]["st-x"]
    assert xo.l1_details.get("revert_failed") == ["X.java"], \
        "清理失败必须机读留痕（revert_failed 写进 l1_details）"
    assert "已清本地树足迹" not in (xo.summary or ""), \
        "树仍脏时摘要绝不能声称『已清本地树足迹』"


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
    # _get_brain_llm 被 __init__ 内的 handle_failure 策略调用 → patch nodes；_proj_path_from_state
    # 被 planning_core 内的 _give_up_preserve_build 调用 → patch pc。
    with patch.object(nodes, "_get_brain_llm", lambda: _L()), \
         patch.object(pc, "_proj_path_from_state", return_value=str(repo)):
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
    with patch("swarm.brain.nodes.recovery._package_in_baseline", return_value=False):
        out = _run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "abandon", "臆造不存在的包应硬失败连坐放弃"
    assert "st-solo" in (out.get("abandoned_subtask_ids") or [])


def test_handle_failure_blocked_pkg_in_baseline_does_not_abandon():
    """假阳性护栏：包【在基线树里】(仅沙箱漏同步) → 不判臆造，继续 transient 等待、不放弃。"""
    state = _blocked_pkg_state("com.ruoyi.common.utils")
    with patch("swarm.brain.nodes.recovery._package_in_baseline", return_value=True):
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
    with patch("swarm.brain.nodes.recovery._package_in_baseline", return_value=False):
        out2 = _run(nodes.handle_failure(state))
    assert out2.get("failure_strategy") != "abandon", "有未放弃生产者的包不可判臆造"


def test_package_in_baseline_detects_present_and_absent(tmp_path):
    """_package_in_baseline 纯函数：存在的包→True，不存在→False，无路径→保守 True。"""
    (tmp_path / "ruoyi-common/src/main/java/com/ruoyi/common/utils").mkdir(parents=True)
    assert nodes._package_in_baseline(str(tmp_path), "com.ruoyi.common.utils") is True
    assert nodes._package_in_baseline(str(tmp_path), "com.ruoyi.common.core.redis") is False
    assert nodes._package_in_baseline(None, "com.x") is True  # 无路径→保守当存在，不误杀
    assert nodes._package_in_baseline(str(tmp_path), "") is True


# ── 拍板项①（v0.9.87）：两条 give_up_preserve 臂同过 #33-闸3 规模闸 ─────────────
# 缺口：阶梯三（replan 守卫内）与签名熔断臂都在 #33-闸3 之前直接 give_up_preserve→PARTIAL，
# 多独立根缺陷（>max(10,25%×计划)=计划覆灭）可绕 escalate 静默清盘。治法：两臂入口同口径
# 闸（_root_defect_ids + mass_abandon_cap 单一事实源），超阈值 escalate 人工。
# ★区分力设计★：不用 mock/spy 证「give_up 没被调」——用真 git 仓库 + 真 _give_up_preserve_build，
# 闸被删时它真会 revert 掉 12 个坏文件并返回 give_up_preserve；故「坏文件仍在树上」=
# 闸确实拦截在 give_up 之前的行为级铁证（突变实验=删闸即红）。


def _mass_root_fixture(tmp_path, n=12, *, with_det_sig=False):
    """n 个独立根缺陷（各自 create 一个坏文件、互不依赖）+ 真实 git 仓库。"""
    from swarm.brain.nodes.failure import _normalize_fail_sig
    repo = _git_repo(tmp_path)
    dfr = "build_fail: ModelParseException Unrecognised tag group"
    roots, results = [], {}
    for i in range(n):
        fn = f"R{i}.java"
        (repo / fn).write_text("BROKEN", encoding="utf-8")
        roots.append(_st(f"st-r{i}", create_files=[fn]))
        results[f"st-r{i}"] = WorkerOutput(
            subtask_id=f"st-r{i}", diff="", summary="", l1_passed=False,
            l1_details=({"det_fail_reason": dfr, "l1_2_compile_ok": False}
                        if with_det_sig else {}))
    return repo, roots, results, _normalize_fail_sig(dfr)


def test_ladder3_mass_roots_gated_to_escalate_not_giveup(tmp_path):
    """★拍板项①本体★：replan 守卫阶梯三路上，12 个独立根缺陷（>cap=max(10,25%×13)=10）
    即使保 build 放弃【可用】（真仓库真文件，give_up 随时能跑）也绝不静默 PARTIAL——
    规模闸先拦截 → escalate 人工；机读 mass_abandon_gate 留痕；坏文件留树未动。"""
    repo, roots, results, _ = _mass_root_fixture(tmp_path)
    done = _st("st-done", writable=["D.java"])
    plan = TaskPlan(subtasks=[*roots, done])
    results["st-done"] = _wo("st-done")

    class _L:
        async def ainvoke(self, _m):
            class _R:
                content = '{"strategy":"replan","reasoning":"修不动"}'
            return _R()

    from swarm.config.settings import get_config
    cap = get_config().model.max_retries
    state = {
        "plan": plan, "project_id": "p1",
        "failed_subtask_ids": [s.id for s in roots],
        "subtask_results": results,
        "subtask_retry_counts": {s.id: cap + 2 for s in roots},  # 耗尽 → 过阶梯一
        "dispatch_remaining": [], "give_up_isolated_ids": [], "abandoned_subtask_ids": [],
    }
    with patch.object(nodes, "_get_brain_llm", lambda: _L()), \
         patch.object(pc, "_proj_path_from_state", return_value=str(repo)):
        out = _run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "escalate", \
        f"12 独立根缺陷=计划覆灭，阶梯三绝不 give_up_preserve 静默清盘: {out.get('failure_strategy')}"
    assert out.get("failure_escalated") is True
    assert any(str(d).startswith("mass_abandon_gate")
               for d in (out.get("degraded_reasons") or [])), out.get("degraded_reasons")
    assert all((repo / f"R{i}.java").exists() for i in range(12)), \
        "闸生效=give_up 被绕过：12 个坏文件绝不能被 revert 清掉（删闸突变此处必红）"


def test_ladder3_below_cap_still_giveup_preserve(tmp_path):
    """★边界不冤杀★：同走阶梯三但独立根缺陷仅 3（≤cap=10）→ 规模闸不触发，
    照常保 build 放弃（give_up_preserve，诚实 PARTIAL），坏文件正常清出树。"""
    repo, roots, results, _ = _mass_root_fixture(tmp_path, n=3)
    done = _st("st-done", writable=["D.java"])
    plan = TaskPlan(subtasks=[*roots, done])
    results["st-done"] = _wo("st-done")

    class _L:
        async def ainvoke(self, _m):
            class _R:
                content = '{"strategy":"replan","reasoning":"修不动"}'
            return _R()

    from swarm.config.settings import get_config
    cap = get_config().model.max_retries
    state = {
        "plan": plan, "project_id": "p1",
        "failed_subtask_ids": [s.id for s in roots],
        "subtask_results": results,
        "subtask_retry_counts": {s.id: cap + 2 for s in roots},
        "dispatch_remaining": [], "give_up_isolated_ids": [], "abandoned_subtask_ids": [],
    }
    with patch.object(nodes, "_get_brain_llm", lambda: _L()), \
         patch.object(pc, "_proj_path_from_state", return_value=str(repo)):
        out = _run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "give_up_preserve", \
        f"3 根缺陷 ≤ 阈值不得误 escalate: {out.get('failure_strategy')}"
    assert not any(str(d).startswith("mass_abandon_gate")
                   for d in (out.get("degraded_reasons") or [])), out.get("degraded_reasons")
    assert not any((repo / f"R{i}.java").exists() for i in range(3)), \
        "未被闸拦：保 build 放弃正常 revert 清足迹"


def test_sig_fuse_mass_roots_gated_to_escalate(tmp_path):
    """★拍板项① sibling（调用点枚举逮到）★：签名熔断臂同型缺口——12 个独立根缺陷同签名
    跨轮复现达 K 次触发 #108 熔断时，超规模阈值同样绝不 PARTIAL 清盘 → escalate 人工。
    （熔断臂在策略分类之前，无需 LLM；删闸突变=真 give_up 清掉坏文件返回 give_up_preserve。）"""
    repo, roots, results, sig = _mass_root_fixture(tmp_path, with_det_sig=True)
    plan = TaskPlan(subtasks=roots)  # 12 子任务 → cap=max(10,3)=10，12>10 触发
    state = {
        "plan": plan, "project_id": "p1",
        "failed_subtask_ids": [s.id for s in roots],
        "subtask_results": results,
        "dispatch_remaining": [], "give_up_isolated_ids": [], "abandoned_subtask_ids": [],
        # 同签名已累计 5 次，本轮 +1=6 ≥ K=6 → 熔断触发（防 .env 残留关门，显式钉开）
        "exec_fail_sig_counts": {sig: 5},
    }
    with patch.dict("os.environ", {"SWARM_EXEC_SIG_FUSE": "1"}), \
         patch.object(pc, "_proj_path_from_state", return_value=str(repo)):
        out = _run(nodes.handle_failure(state))
    assert out.get("failure_strategy") == "escalate", \
        f"熔断臂 12 独立根缺陷=计划覆灭，绝不 give_up_preserve 静默清盘: {out.get('failure_strategy')}"
    assert out.get("failure_escalated") is True
    assert any(str(d).startswith("mass_abandon_gate")
               for d in (out.get("degraded_reasons") or [])), out.get("degraded_reasons")
    assert all((repo / f"R{i}.java").exists() for i in range(12)), \
        "闸生效=熔断臂 give_up 被绕过：坏文件绝不能被 revert（删闸突变此处必红）"


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
