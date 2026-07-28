"""R67L-B4（22号文批次4）行为测试：编排层软掉账/幽灵解封族四件套。

round67l 三路复盘定案的编排层治本：
  ① retry 组内饥饿者优先/死结降权（骨牌2：st-2/8/14 死结 4 轮占槽、11 个 retry
     兑现者 totals=1 饿死 70min，终态 dispatched_unaccounted 认账）；
  ② 模块 pom 生产者→消费者依赖边确定性补齐（st-14 file_plan 声明空→首批即派
     =未授权序执行，越权写根 pom 钉 3.8.7+注册不存在模块毒终态树）；
  ③ T7 终态清扫洞：失败子任务越权写 scope 外文件（自身 result diff 为证）并入清扫面；
  ④ 账簿双计：桩完成者（give_up∩completed）不计放弃账，单列披露。
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import derive_module_pom_producer_edges  # noqa: E402
from swarm.brain.nodes.planning_core import _local_tree_revert_subtask  # noqa: E402
from swarm.brain.nodes.shared import partition_abandoned_account  # noqa: E402
from swarm.brain.plan_finisher import wire_module_pom_dep_edges  # noqa: E402
from swarm.types import (FileScope, SubTask, SubTaskDifficulty, TaskHarness,  # noqa: E402
                         TaskIntent, TaskPlan)


def _st(sid, *, writable=None, create=None, readable=None, intent=None,
        verify=None, depends_on=None, desc="x"):
    return SubTask(
        id=sid, description=desc, difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=writable or [], create_files=create or [],
                        readable=readable or []),
        harness=TaskHarness(language="java", build_command="mvn -q compile",
                            verify_commands=verify or []),
        intent=intent or TaskIntent.MODIFY,
        depends_on=depends_on or [],
    )


def _plan(*sts):
    return TaskPlan(subtasks=list(sts))


# ─── ① retry 组内饥饿者优先/死结降权 ───


def _starvation_plan():
    """round67l 骨牌2 原型：全就绪 retry 组——死结 st-2/8/14（totals=3/4/3）vs
    饥饿兑现者 st-73/74/75/76（totals=1）。"""
    sts = [_st(sid) for sid in ("st-2", "st-8", "st-14",
                                "st-73", "st-74", "st-75", "st-76")]
    plan = _plan(*sts)
    remaining = [st.id for st in sts]
    deprio = set(remaining)  # 全部 retry_counts>0（Fix F 降优先级组）
    totals = {"st-2": 3, "st-8": 4, "st-14": 3,
              "st-73": 1, "st-74": 1, "st-75": 1, "st-76": 1}
    return plan, remaining, deprio, totals


def test_retry_group_starvation_first():
    """R67L-B4①：totals 升序——饥饿兑现者（1 次）先占槽，死结（3-4 次）沉底。"""
    plan, remaining, deprio, totals = _starvation_plan()
    batch = plan.get_dispatch_batch(set(), remaining, 4, deprioritized=deprio,
                                    dispatch_totals=totals)
    ids = [t.id for t in batch]
    assert ids == ["st-73", "st-74", "st-75", "st-76"], \
        "4 个槽必须全给 totals=1 的饥饿者（死结 st-2/8/14 沉底填剩余槽——此处无剩余）"


def test_retry_group_deadend_fills_leftover_slots():
    """死结降权非放弃：槽位多于饥饿者时死结仍按 totals 升序填剩余槽。"""
    plan, remaining, deprio, totals = _starvation_plan()
    batch = plan.get_dispatch_batch(set(), remaining, 6, deprioritized=deprio,
                                    dispatch_totals=totals)
    ids = [t.id for t in batch]
    assert ids[:4] == ["st-73", "st-74", "st-75", "st-76"]
    assert set(ids[4:]) == {"st-2", "st-14"}, "死结填剩余槽（totals=3 先于 totals=4 的 st-8）"
    assert "st-8" not in ids


def test_retry_group_no_totals_legacy_order():
    """dispatch_totals 缺省=完全等价旧行为（plan 原序截断）。"""
    plan, remaining, deprio, _totals = _starvation_plan()
    batch = plan.get_dispatch_batch(set(), remaining, 3, deprioritized=deprio)
    assert [t.id for t in batch] == ["st-2", "st-8", "st-14"]


def test_fresh_frontier_still_beats_starved_retry():
    """Fix F 语义不变：新前沿（fresh）恒优先于 retry 组（不论 totals）。"""
    fresh = _st("st-new")
    retry = _st("st-old")
    plan = _plan(retry, fresh)  # retry 在 plan 序更靠前
    batch = plan.get_dispatch_batch(
        set(), ["st-old", "st-new"], 2, deprioritized={"st-old"},
        dispatch_totals={"st-old": 1})
    assert [t.id for t in batch] == ["st-new", "st-old"]


def test_starvation_sort_stable_same_totals():
    """同 totals 保持 plan 原序（稳定排序确定性）。"""
    a, b, c = _st("st-a"), _st("st-b"), _st("st-c")
    plan = _plan(a, b, c)
    batch = plan.get_dispatch_batch(
        set(), ["st-a", "st-b", "st-c"], 3, deprioritized={"st-a", "st-b", "st-c"},
        dispatch_totals={"st-a": 2, "st-b": 2, "st-c": 1})
    assert [t.id for t in batch] == ["st-c", "st-a", "st-b"]


# ─── ② 模块 pom 生产者→消费者依赖边 ───

_DIRS = {"ruoyi-alarm": "ruoyi-alarm", "ruoyi-alarm-interface": "ruoyi-alarm-interface",
         "ruoyi-common": "ruoyi-common"}


def _pom_chain_plan():
    """st-prod 持 ruoyi-alarm-interface/pom.xml；st-cons 持 ruoyi-alarm/pom.xml，
    且 alarm 代码子任务 readable→interface code 文件（编译依赖证据）。"""
    prod = _st("st-prod", create=["ruoyi-alarm-interface/pom.xml"])
    cons = _st("st-cons", create=["ruoyi-alarm/pom.xml"])
    code = _st("st-code",
               create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/Engine.java"],
               readable=["ruoyi-alarm-interface/src/main/java/com/ruoyi/alarm/client/Client.java"])
    return _plan(prod, cons, code)


def test_module_pom_edge_derived_from_evidence():
    """round67l st-14 原型：LLM 声明缺口 → 证据推导 cons pom 等 prod pom。"""
    plan = _pom_chain_plan()
    spec = derive_module_pom_producer_edges(plan, _DIRS)
    assert spec == {"st-cons": ["st-prod"]}


def test_module_pom_edge_wired_with_cycle_guard():
    """wire pass：补边落 depends_on 且幂等；反向（生产者已依赖消费者）成环跳过。"""
    plan = _pom_chain_plan()
    added = wire_module_pom_dep_edges(plan, _DIRS)
    assert added == {"st-cons": ["st-prod"]}
    assert plan.subtasks[1].depends_on == ["st-prod"]
    assert wire_module_pom_dep_edges(plan, _DIRS) == {}, "幂等：再跑零变动"


def test_module_pom_edge_cycle_skipped():
    """生产者传递依赖消费者 → 成环不猜（同 T4b 律，留结构闸面）。"""
    plan = _pom_chain_plan()
    plan.subtasks[0].depends_on = ["st-cons"]  # prod 反依赖 cons
    assert wire_module_pom_dep_edges(plan, _DIRS) == {}
    assert plan.subtasks[1].depends_on == []


def test_module_pom_mutual_pair_skipped():
    """互指模块（A↔B 各有对方证据）双向不补边（同 T5 律）。"""
    a_pom = _st("st-a-pom", create=["mod-a/pom.xml"])
    b_pom = _st("st-b-pom", create=["mod-b/pom.xml"])
    a_code = _st("st-a-code", create=["mod-a/src/main/java/A.java"],
                 readable=["mod-b/src/main/java/B.java"])
    b_code = _st("st-b-code", create=["mod-b/src/main/java/B.java"],
                 readable=["mod-a/src/main/java/A.java"])
    plan = _plan(a_pom, b_pom, a_code, b_code)
    spec = derive_module_pom_producer_edges(plan, {"mod-a": "mod-a", "mod-b": "mod-b"})
    assert spec == {}, "互指=更深计划错，双向跳过"


def test_module_pom_baseline_dep_no_edge():
    """依赖基线模块（无 plan owner）不产边——基线 pom 本就存在。"""
    cons = _st("st-cons", create=["ruoyi-alarm/pom.xml"])
    code = _st("st-code",
               create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/Engine.java"],
               readable=["ruoyi-common/src/main/java/com/ruoyi/common/core/R.java"])
    plan = _plan(cons, code)
    spec = derive_module_pom_producer_edges(
        plan, {"ruoyi-alarm": "ruoyi-alarm", "ruoyi-common": "ruoyi-common"})
    assert spec == {}, "ruoyi-common 无 pom owner（基线）→ 不产边"


def test_module_pom_aux_resource_not_evidence():
    """R65E14-T5 同律：AUX 资源（static/*.js）不构成编译依赖证据。"""
    prod = _st("st-prod", create=["mod-b/pom.xml"])
    cons = _st("st-cons", create=["mod-a/pom.xml"])
    code = _st("st-code", create=["mod-a/src/main/java/A.java"],
               readable=["mod-b/src/main/resources/static/app.js"])
    plan = _plan(prod, cons, code)
    spec = derive_module_pom_producer_edges(plan, {"mod-a": "mod-a", "mod-b": "mod-b"})
    assert spec == {}


# ─── ③ T7 清扫洞：extra_files 并入 revert 面 ───


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "pom.xml").write_text("<project><!-- base --></project>\n")
    (repo / "A.java").write_text("class A {}\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def test_revert_extra_files_out_of_scope(tmp_path):
    """round67l st-14 原型：scope 仅 ruoyi-alarm/pom.xml，越权写根 pom（tracked 脏）
    +新建 scope 外文件（untracked）——extra_files 并入清扫面后两者都被清。"""
    repo = _git_repo(tmp_path)
    # worker 越权：改根 pom（tracked）+ 新建 scope 外文件（untracked）
    (repo / "pom.xml").write_text("<project><module>ruoyi-alarm</module></project>\n")
    (repo / "Ghost.java").write_text("class Ghost {}\n")
    st = _st("st-14", create=["ruoyi-alarm/pom.xml"])
    r = _local_tree_revert_subtask(
        str(repo), st, protected_files=set(), base_ref=None,
        extra_files=["pom.xml", "Ghost.java"])
    assert "pom.xml" in r["reverted"], "越权 tracked 改动必须 checkout 还原"
    assert "Ghost.java" in r["removed"], "越权 untracked 产物必须删除"
    assert (repo / "pom.xml").read_text() == "<project><!-- base --></project>\n"
    assert not (repo / "Ghost.java").exists()


def test_revert_extra_files_protected_sibling_untouched(tmp_path):
    """extra_files 同过 protected 窄守卫：完成兄弟的产物绝不误删。"""
    repo = _git_repo(tmp_path)
    (repo / "pom.xml").write_text("<project><!-- sibling 完成者改动 --></project>\n")
    st = _st("st-fail", create=["m/pom.xml"])
    r = _local_tree_revert_subtask(
        str(repo), st, protected_files={"pom.xml"}, base_ref=None,
        extra_files=["pom.xml"])
    assert r["reverted"] == [] and "pom.xml" in r["skipped_protected"]
    assert "sibling" in (repo / "pom.xml").read_text()


def test_revert_no_extra_files_legacy_behavior(tmp_path):
    """extra_files 缺省=纯 scope 驱动（旧行为）：scope 外脏文件不动。"""
    repo = _git_repo(tmp_path)
    (repo / "pom.xml").write_text("<project><!-- dirty --></project>\n")
    st = _st("st-fail", create=["m/pom.xml"])
    r = _local_tree_revert_subtask(str(repo), st, protected_files=set(), base_ref=None)
    assert r["reverted"] == [] and r["removed"] == []
    assert "dirty" in (repo / "pom.xml").read_text()


# ─── ④ 账簿双计：桩完成者不计放弃账 ───


def test_partition_stub_completed_not_double_counted():
    """round67l st-3-1 原型：give_up∩completed（桩 l1_passed=True）→ 真放弃=0、桩披露=1。"""
    true_ab, stub = partition_abandoned_account(
        abandoned_ids=[], give_up_ids=["st-3-1"],
        completed_ids={"st-3-1", "st-8"})
    assert true_ab == set(), "桩完成者不得计放弃账（旧口径双计：完成4+放弃1 同 id）"
    assert stub == {"st-3-1"}


def test_partition_real_abandoned_unaffected():
    """真放弃（give_up 但无产出/revert 路）照常计放弃账。"""
    true_ab, stub = partition_abandoned_account(
        abandoned_ids=["st-a"], give_up_ids=["st-b", "st-c"],
        completed_ids={"st-c"})
    assert true_ab == {"st-a", "st-b"}
    assert stub == {"st-c"}


def test_partition_empty_inputs():
    true_ab, stub = partition_abandoned_account([], [], set())
    assert true_ab == set() and stub == set()
