"""R65REPLAY-T7（task #72）：终态诚实清扫——L1-fail 产物不得留在交付树。

round65d 回放轮 C 路 mvn 实证：有账 24 子任务产物 100% 编译通过；盘上全部编译破坏点
来自"账外幽灵件"——首派失败（L1 fail）产物 15 件经 pull-back 落进共享本地树（DutySchedule
Job Calendar.getCalendar()×2 / AlarmSimpleUtil 截断 import / AlarmHttpClient 幻影 API），
毁 ruoyi-alarm 与 interface 两模块 mvn compile。剔除 15 件后交付树极高概率整体可编译。

治本边界：pull-back 本身不动（B2 分批续作/重试/取证依赖失败产物在树）；#62 治了 merge/
交付面；本案治【终态】——PARTIAL/FAILED 结算前对"派发过（dispatch_totals>0）且未完成
（非 l1_passed）"的计划内子任务跑 _local_tree_revert_subtask（H2 同源回滚器）：
tracked→checkout 钉扎 base、untracked→删除；protected=全部完成者 diff 产物（绝不误删
兄弟好产物，H-exec2 守卫复用）；DONE 不清扫（走 merge+#62）；fail-open。
机读账 state["unverified_footprints_swept"] → _failed_machine_account 发键。栈中立。
"""
from __future__ import annotations

import subprocess

import pytest

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput


def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "base.txt").write_text("baseline\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _st(sid, create=None, writable=None):
    return SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=writable or []),
                   depends_on=[])


def _mk_state(repo):
    good = _st("st-good", create=["mod/Good.java"])
    bad = _st("st-bad", create=["mod/Bad.java"], writable=["base.txt"])
    never = _st("st-never", create=["mod/Never.java"])
    plan = TaskPlan(subtasks=[good, bad, never],
                    parallel_groups=[["st-good", "st-bad", "st-never"]])
    # 盘面：完成者产物 + 失败者幽灵件（untracked）+ 失败者污染 tracked 基线文件
    (repo / "mod").mkdir()
    (repo / "mod" / "Good.java").write_text("class Good {}\n")
    (repo / "mod" / "Bad.java").write_text("class, interface, enum expected\n")
    (repo / "base.txt").write_text("poisoned by st-bad\n")
    return {
        "plan": plan,
        "subtask_results": {
            "st-good": WorkerOutput(
                subtask_id="st-good", summary="", l1_passed=True,
                diff="diff --git a/mod/Good.java b/mod/Good.java\nnew file mode 100644\n--- /dev/null\n+++ b/mod/Good.java\n@@ -0,0 +1 @@\n+class Good {}\n"),
        },
        "subtask_dispatch_totals": {"st-good": 1, "st-bad": 2},
        "abandoned_subtask_ids": [],
        "base_commit": None,
    }


def test_sweep_removes_failed_footprint_keeps_completed(repo):
    """★幽灵件本体★：派发过未完成者的 untracked 产物删除、tracked 污染还原；
    完成者产物一根毫毛不动；未派发者不碰。"""
    from swarm.brain.runner import _sweep_unverified_footprints
    state = _mk_state(repo)
    out = _sweep_unverified_footprints("t-1", state, project_path=str(repo))
    assert not (repo / "mod" / "Bad.java").exists(), \
        "L1-fail 幽灵件留在交付树=回放轮两模块 mvn compile 全红死型"
    assert (repo / "base.txt").read_text() == "baseline\n", \
        "失败者污染的 tracked 文件必须还原钉扎基线"
    assert (repo / "mod" / "Good.java").exists(), "完成者产物被误删=冤杀"
    assert "st-bad" in (out.get("swept_subtasks") or []), out
    assert state.get("unverified_footprints_swept"), "机读账必须落 state 供终态账拾取"


def test_sweep_protects_completed_overlap(repo):
    """footprint 与完成者产物重叠（同文件多写者）→ H-exec2 守卫跳过，绝不误删。"""
    from swarm.brain.runner import _sweep_unverified_footprints
    state = _mk_state(repo)
    # st-bad 的 create_files 里也声明了完成者的 Good.java（计划双写形态）
    state["plan"].subtasks[1].scope.create_files.append("mod/Good.java")
    _sweep_unverified_footprints("t-1", state, project_path=str(repo))
    assert (repo / "mod" / "Good.java").exists(), "完成者 diff 产物必须被 protected"


def test_sweep_noop_without_dispatch(repo):
    """零派发（FAILED@PLAN 形态）→ 零清扫零机读账。"""
    from swarm.brain.runner import _sweep_unverified_footprints
    state = _mk_state(repo)
    state["subtask_dispatch_totals"] = {}
    out = _sweep_unverified_footprints("t-1", state, project_path=str(repo))
    assert not (out.get("swept_subtasks") or [])
    assert (repo / "mod" / "Bad.java").exists()   # 没派发过就不是本闸的事


def test_machine_account_picks_up_sweep(repo):
    """终态机读账拾取清扫结果（有才发键）。"""
    from swarm.brain.runner import _failed_machine_account, _sweep_unverified_footprints
    state = _mk_state(repo)
    _sweep_unverified_footprints("t-1", state, project_path=str(repo))
    tu = _failed_machine_account("t-1", state, "rejected_partial")
    assert tu.get("unverified_footprints_swept"), tu


# ───────── 双复核整改锁 ─────────

def test_protected_includes_completed_scope_declaration(repo):
    """复核 F1（hunter/reviewer 双报）：完成者 scope 声明的文件即使不在其 diff 文本里
    （同内容 rename 无 hunk 形态）也必须受保护——protected=diff 真账∪scope 声明。"""
    from swarm.brain.runner import _sweep_unverified_footprints
    state = _mk_state(repo)
    # 完成者 st-good 的 scope 再声明一个 diff 里没有的文件；失败者也声明它
    (repo / "mod" / "Extra.java").write_text("class Extra {}\n")
    state["plan"].subtasks[0].scope.create_files.append("mod/Extra.java")
    state["plan"].subtasks[1].scope.create_files.append("mod/Extra.java")
    _sweep_unverified_footprints("t-1", state, project_path=str(repo))
    assert (repo / "mod" / "Extra.java").exists(), \
        "完成者 scope 声明未进 protected → rename/空 hunk 形态被失败兄弟连坐误删"


def test_unsweepable_plan_external_id_accounted(repo):
    """复核 F5：plan 外旧 id（重拆前父）派发过但无 scope 可清 → unsweepable 留痕入账。"""
    from swarm.brain.runner import _sweep_unverified_footprints
    state = _mk_state(repo)
    state["subtask_dispatch_totals"]["st-old-1"] = 1
    out = _sweep_unverified_footprints("t-1", state, project_path=str(repo))
    assert out.get("unsweepable") == ["st-old-1"], out
    assert "st-old-1" in (state.get("unverified_footprints_swept") or {}).get(
        "unsweepable", []), "账与扫帚的分歧必须落机读账"


def test_midloop_failure_keeps_partial_account(repo, monkeypatch):
    """复核 F6：单子任务清扫抛异常 → 其余继续、已扫账+sweep_errors 仍落 state。"""
    import swarm.brain.runner as rn
    state = _mk_state(repo)
    # 两个失败者：st-bad + st-bad2（后者清扫时抛）
    bad2 = state["plan"].subtasks[2]          # st-never 改造成已派发者
    state["subtask_dispatch_totals"]["st-never"] = 1
    (repo / "mod" / "Never.java").write_text("garbage\n")
    real = __import__("swarm.brain.nodes.planning_core",
                      fromlist=["_local_tree_revert_subtask"])._local_tree_revert_subtask

    def _boom(project_path, st, protected_files=None, base_ref=None):
        if getattr(st, "id", "") == "st-bad":
            raise RuntimeError("boom")
        return real(project_path, st, protected_files=protected_files, base_ref=base_ref)

    import swarm.brain.nodes.planning_core as pc
    monkeypatch.setattr(pc, "_local_tree_revert_subtask", _boom)
    out = rn._sweep_unverified_footprints("t-1", state, project_path=str(repo))
    assert "st-bad" in (out.get("sweep_errors") or []), out
    assert not (repo / "mod" / "Never.java").exists(), "异常者之外的清扫必须继续"
    acct = state.get("unverified_footprints_swept") or {}
    assert acct.get("sweep_errors") == ["st-bad"], \
        f"部分清扫+错误必须落账（清了无人知道=本案要治的病）: {acct}"
