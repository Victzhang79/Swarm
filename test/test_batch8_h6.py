"""H-6（SPEC_h6_file_plan_reconciliation）：file_plan 权威对账收缩总闸行为测试。

- 裁决账（file_plan_adjudications）：append-only、(action,path) 去重、BrainState 声明+生命周期登记。
- reconcile_file_plan_ledger：裁决重放（精确路径残留/复活删除）+ 膨胀收缩（JVM 同串变体删除，
  owner 落点豁免，同名不同串放行）+ 幂等。
- attach_orphan_file_plan_entries 前置核：裁决路径不挂靠不进 left（left 会触发孤儿承接=另一复活面）。
- 生命周期：REVISE/replan 新周期清账对称。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import (  # noqa: E402
    _record_adjudication,
    _strip_file_plan_create_entries,
    adjudicated_path_set,
    reconcile_file_plan_ledger,
)
from swarm.brain.symbol_surgery import attach_orphan_file_plan_entries  # noqa: E402
from swarm.types import FileScope, SubTask, TaskPlan  # noqa: E402

_J = "ruoyi-alarm/src/main/java/com/ruoyi/alarm"
_JC = "ruoyi-common/src/main/java/com/ruoyi/common"


def _st(sid, creates=(), module_dir="ruoyi-alarm"):
    return SubTask(id=sid, description=f"{sid} desc",
                   scope=FileScope(create_files=list(creates), writable=list(creates)),
                   acceptance_criteria=["ok"])


# ── 裁决账 ──────────────────────────────────────────────
def test_ledger_append_dedupes_same_action_path():
    ledger: list = []
    _record_adjudication(ledger, pass_name="R67F-samename", action="strip",
                         path=f"{_J}/util/AesUtils.java",
                         owner_path=f"{_JC}/utils/encrypt/AesUtils.java", round_no=1)
    _record_adjudication(ledger, pass_name="R67F-samename", action="strip",
                         path=f"./{_J}/util/AesUtils.java",  # ./ 变体=同一路径
                         owner_path=f"{_JC}/utils/encrypt/AesUtils.java", round_no=2)
    assert len(ledger) == 1, "(action,path) 去重保首次——跨 retry 轮重判不胀账"
    assert ledger[0]["round"] == 1 and ledger[0]["pass"] == "R67F-samename"


def test_strip_records_adjudication_even_without_file_plan():
    """H-1 早退（file_plan 缺失）也必须先入账——attach 前置核/对账收缩读的是账。"""
    ledger: list = []
    n = _strip_file_plan_create_entries(None, {"a/b/C.java": "a/c/C.java"},
                                        adjudications=ledger, pass_name="#101-xmod")
    assert n == 0 and len(ledger) == 1
    assert ledger[0]["action"] == "strip" and ledger[0]["path"] == "a/b/C.java"


def test_ledger_key_declared_and_registered():
    """复核点名：账键 BrainState 声明 + 生命周期登记（未声明=LangGraph 静默丢弃）。"""
    import typing

    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState
    hints = typing.get_type_hints(BrainState, include_extras=True)
    assert "file_plan_adjudications" in hints
    assert ACCOUNTING_KEY_LIFECYCLE["file_plan_adjudications"] == "monotonic"


# ── reconcile：裁决重放 + 膨胀收缩 ──────────────────────
def _adjs():
    return [{"round": 1, "pass": "R67F-samename", "action": "strip",
             "path": f"{_J}/util/AesUtils.java",
             "owner_path": f"{_JC}/utils/encrypt/AesUtils.java"}]


def test_reconcile_replays_strip_on_resurrected_path():
    """红灯1·轮2：被剥路径在 file_plan 复活（重拆/回退残留）→ 重放删除 + depends_on 改指 owner。"""
    fp = [
        {"path": f"{_JC}/utils/encrypt/AesUtils.java", "action": "create"},
        {"path": f"{_J}/util/AesUtils.java", "action": "create"},          # 复活副本
        {"path": f"{_J}/service/AlarmService.java", "action": "create",
         "depends_on": [f"{_J}/util/AesUtils.java"]},
    ]
    counts = reconcile_file_plan_ledger(fp, _adjs())
    assert counts["adjudications_replayed"] == 1
    paths = [e["path"] for e in fp]
    assert f"{_J}/util/AesUtils.java" not in paths
    assert f"{_JC}/utils/encrypt/AesUtils.java" in paths, "owner 条目绝不动"
    assert fp[1]["depends_on"] == [f"{_JC}/utils/encrypt/AesUtils.java"], "depends_on 改指 owner"


def test_reconcile_shrinks_same_fqn_variant_but_keeps_owner():
    """膨胀收缩：同串（同 fqn）变体路径=复活面 → 删；owner 落点豁免；同名不同串放行。"""
    fp = [
        {"path": f"{_JC}/utils/encrypt/AesUtils.java", "action": "create"},   # owner
        {"path": f"{_J}/util/AesUtils.java", "action": "create"},             # 被剥路径本身（重放）
        {"path": "ruoyi-admin/src/main/java/com/ruoyi/alarm/util/AesUtils.java",
         "action": "create"},  # 同串变体：同错 fqn（com/ruoyi/alarm/util）换模块目录重发明
        {"path": f"{_J}/util/AesHelper.java", "action": "create"},            # 不同名
    ]
    counts = reconcile_file_plan_ledger(fp, _adjs())
    paths = [e["path"] for e in fp]
    # 异包副本（恰是被剥路径本身）重放删除；同 fqn 变体收缩删除
    assert counts["adjudications_replayed"] == 1 and counts["new_entries_shrunk"] == 1
    assert f"{_JC}/utils/encrypt/AesUtils.java" in paths, "owner 豁免"
    assert f"{_J}/util/AesHelper.java" in paths, "不同名不受影响"


def test_reconcile_same_basename_different_package_allowed():
    """复核点名：粘滞误拒合法新文件——同名不同串（不同包=不同 fqn）必须放行。"""
    fp = [
        {"path": f"{_JC}/utils/encrypt/AesUtils.java", "action": "create"},
        {"path": f"{_J}/helper/AesUtils.java", "action": "create"},   # 同名不同包=合法新类
    ]
    counts = reconcile_file_plan_ledger(fp, _adjs())
    assert counts["adjudications_replayed"] == 0 and counts["new_entries_shrunk"] == 0
    assert len(fp) == 2, "同名不同串绝不误拒（round67c 血泪方向）"


def test_reconcile_idempotent():
    fp = [{"path": f"{_J}/util/AesUtils.java", "action": "create"}]
    c1 = reconcile_file_plan_ledger(fp, _adjs())
    c2 = reconcile_file_plan_ledger(fp, _adjs())
    assert c1["adjudications_replayed"] == 1
    assert c2 == {"adjudications_replayed": 0, "new_entries_shrunk": 0}, "重放两次=一次"


def test_reconcile_non_jvm_exact_path_only():
    """栈中立：非 JVM 路径（无 fqn）只受精确路径约束——同名跨目录合法（Go/Py/TS 语义）。"""
    adjs = [{"round": 1, "pass": "#101-xmod", "action": "strip",
             "path": "svc-a/internal/util/retry.go", "owner_path": "svc-b/internal/util/retry.go"}]
    fp = [
        {"path": "svc-b/internal/util/retry.go", "action": "create"},   # owner
        {"path": "svc-a/internal/util/retry.go", "action": "create"},   # 精确命中 → 删
        {"path": "svc-c/internal/util/retry.go", "action": "create"},   # 同名不同目录 → 放行
    ]
    counts = reconcile_file_plan_ledger(fp, adjs)
    assert counts["adjudications_replayed"] == 1 and counts["new_entries_shrunk"] == 0
    assert [e["path"] for e in fp] == ["svc-b/internal/util/retry.go",
                                       "svc-c/internal/util/retry.go"]


def test_reconcile_modify_action_untouched():
    """modify 条目不受 strip 裁决影响（CVB 归位后的 base modify 是合法形态）。"""
    fp = [{"path": f"{_J}/util/AesUtils.java", "action": "modify"}]
    counts = reconcile_file_plan_ledger(fp, _adjs())
    assert counts == {"adjudications_replayed": 0, "new_entries_shrunk": 0}
    assert len(fp) == 1


# ── attach 前置核（C-4 治）──────────────────────────────
def test_attach_refuses_adjudicated_path_and_left_clean():
    """红灯1·挂靠面：裁决路径不挂靠【也不进 left】（left→孤儿承接新建=另一复活通道）。"""
    plan = TaskPlan(subtasks=[_st("st-1", [f"{_J}/service/AlarmService.java"])],
                    parallel_groups=[["st-1"]])
    paths = [f"{_J}/util/AesUtils.java", f"{_J}/mapper/AlarmMapper.java"]
    attached, left = attach_orphan_file_plan_entries(plan, paths, adjudications=_adjs())
    assert f"{_J}/util/AesUtils.java" not in plan.subtasks[0].scope.create_files, "裁决路径拒挂"
    assert f"{_J}/util/AesUtils.java" not in left, "拒挂路径不进 left（防孤儿承接复活）"
    assert f"{_J}/mapper/AlarmMapper.java" in plan.subtasks[0].scope.create_files, "真孤儿照挂"
    assert attached == 1


def test_attach_without_adjudications_unchanged():
    """零回归：无裁决账时挂靠行为原样。"""
    plan = TaskPlan(subtasks=[_st("st-1", [f"{_J}/service/AlarmService.java"])],
                    parallel_groups=[["st-1"]])
    attached, left = attach_orphan_file_plan_entries(plan, [f"{_J}/util/AesUtils.java"])
    assert attached == 1 and left == []


# ── 生命周期：REVISE 清账对称 ───────────────────────────
async def test_revision_clears_and_rederives_adjudications(monkeypatch):
    """复核点名：REVISE=新周期 → 返回键必带 file_plan_adjudications（清空后由 resolve 重推导）。"""
    import swarm.brain.nodes as nodes
    from swarm.types import WorkerOutput

    class _RevLLM:
        async def ainvoke(self, messages):
            class R:
                content = ('{"revision_subtasks": [{"id": "rev-1", "description": "按反馈修改",'
                           ' "scope": {"writable": ["a.x"], "readable": []}}]}')
            return R()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _RevLLM())
    wo = WorkerOutput(subtask_id="st-1", success=True, diff="d", summary="s")
    out = await nodes.revision({
        "revision_feedback": "改一下", "merged_diff": "", "task_description": "t",
        "plan": TaskPlan(subtasks=[_st("st-1", ["a.x"])], parallel_groups=[["st-1"]]),
        "subtask_results": {"st-1": wo}, "project_id": "",
        "file_plan_adjudications": [{"round": 3, "pass": "R67F-samename", "action": "strip",
                                     "path": "x/Y.java", "owner_path": "z/Y.java"}],
    })
    assert "file_plan_adjudications" in out, "REVISE 必须对称清账（always-emit）"
    # 本场景 resolve 无新裁决（无冲突面）→ 清成空账；旧账（round=3 粘滞）不得残留
    assert out["file_plan_adjudications"] == []


def test_adjudicated_path_set_empty_owner_never_blocks_valid_owner():
    """批次8 闸门 reviewer MEDIUM 整改④：同 fqn 空 owner 先行不得占位（setdefault）——
    后续同 fqn 的有效 owner 必须入映射，否则膨胀收缩对该 fqn 永久失明。"""
    from swarm.brain.contract_utils import adjudicated_path_set
    p1 = "ruoyi-admin/src/main/java/com/ruoyi/alarm/util/AesUtils.java"
    p2 = "ruoyi-common/src/main/java/com/ruoyi/alarm/util/AesUtils.java"
    owner = "ruoyi-common/src/main/java/com/ruoyi/common/utils/encrypt/AesUtils.java"

    paths, fqns = adjudicated_path_set([
        {"pass": "R67G-fileplan", "action": "dedupe", "path": p1, "owner_path": ""},
        {"pass": "R67G-fileplan", "action": "dedupe", "path": p2, "owner_path": owner},
    ])
    assert {p1, p2} <= paths, "空 owner 的 path 仍受精确路径集约束"
    assert owner in fqns.values(), "空 owner 占位会让后续有效 owner 永远无法修正"
    assert len(fqns) == 1, "p1/p2 同 fqn 同包 → 映射只一条"

    # 对照：只有空 owner → fqn 映射不得出现（空串绝不占位），path 集照常
    paths2, fqns2 = adjudicated_path_set([
        {"pass": "R67G-fileplan", "action": "dedupe", "path": p1, "owner_path": ""}])
    assert p1 in paths2 and not fqns2


def test_record_adjudication_empty_owner_yields_to_valid():
    """闸门 R2 reviewer LOW④：同 (action,path) 去重保首次，但空 owner 必须让位给后续
    有效 owner——否则 adjudicated_path_set 对该 fqn 的膨胀收缩永久失明。"""
    from swarm.brain.contract_utils import _record_adjudication, adjudicated_path_set
    ledger: list = []
    p2 = "ruoyi-common/src/main/java/com/ruoyi/alarm/util/AesUtils.java"
    owner = "ruoyi-common/src/main/java/com/ruoyi/common/utils/encrypt/AesUtils.java"
    _record_adjudication(ledger, pass_name="R67G-fileplan", action="dedupe",
                         path=p2, owner_path=None)
    _record_adjudication(ledger, pass_name="R67G-fileplan", action="dedupe",
                         path=p2, owner_path=owner)
    assert len(ledger) == 1, "去重保首次：同 (action,path) 不重复入账"
    assert ledger[0]["owner_path"] == owner, "空 owner 不得遮挡后续有效 owner"
    _, fqns = adjudicated_path_set(ledger)
    assert owner in fqns.values(), "修正后的 owner 必须进 fqn 映射（收缩不失明）"
