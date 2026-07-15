"""G2（Task#9 审计③ GAP1）：readable 消费 → depends_on 供给边 provenance 自愈。"""
from __future__ import annotations

from swarm.brain.contract_utils import wire_readable_provenance
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _mk(sid, *, create=None, readable=None, deps=None):
    return SubTask(
        id=sid, description=f"sub {sid}", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=create or [], writable=[], readable=readable or []),
        depends_on=deps or [], acceptance_criteria=["ok"])


def _plan(subs):
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]], shared_contract={})


def test_g2_missing_edge_added():
    """B.readable 引 A 的 create 文件、但无依赖边 → 补 B→A。"""
    f = "mod-a/src/main/java/A.java"
    plan = _plan([_mk("A", create=[f]), _mk("B", readable=[f])])
    added, cyc = wire_readable_provenance(plan)
    assert ("B", "A") in added
    b = next(s for s in plan.subtasks if s.id == "B")
    assert "A" in b.depends_on
    assert not cyc


def test_g2_existing_direct_edge_no_change():
    f = "mod-a/src/main/java/A.java"
    plan = _plan([_mk("A", create=[f]), _mk("B", readable=[f], deps=["A"])])
    added, cyc = wire_readable_provenance(plan)
    assert not added and not cyc


def test_g2_transitive_edge_no_change():
    """B →(既有) M →(既有) A，B.readable 引 A 产物 → 传递已通，不重复加边。"""
    f = "mod-a/src/main/java/A.java"
    plan = _plan([_mk("A", create=[f]), _mk("M", deps=["A"]), _mk("B", readable=[f], deps=["M"])])
    added, cyc = wire_readable_provenance(plan)
    assert not added and not cyc


def test_g2_cycle_not_created():
    """A 已依赖 B（A→B），B.readable 又引 A 的产物 → 加 B→A 会成环 → 不加、记 unresolved。"""
    f = "mod-a/src/main/java/A.java"
    plan = _plan([_mk("A", create=[f], deps=["B"]), _mk("B", readable=[f])])
    added, cyc = wire_readable_provenance(plan)
    assert ("B", "A") in cyc
    assert not added
    b = next(s for s in plan.subtasks if s.id == "B")
    assert "A" not in b.depends_on   # 绝不制造环


def test_g2_baseline_readable_ignored():
    """readable 引的是无任何 producer 的基线只读文件 → 不加边。"""
    plan = _plan([_mk("A", create=["mod-a/src/main/java/A.java"]),
                  _mk("B", readable=["ruoyi-common/src/main/java/Base.java"])])
    added, cyc = wire_readable_provenance(plan)
    assert not added and not cyc


def test_g2_single_subtask_noop():
    plan = _plan([_mk("A", create=["a.java"], readable=["a.java"])])
    added, cyc = wire_readable_provenance(plan)
    assert not added and not cyc


def test_g2_ambiguous_producer_skipped(caplog):
    """复核 HIGH：同一文件不同拼写被两个子任务 create（normalize 键空间漏归一）→ 归一后同键
    多产者 → 跳过接线（绝不挂任意产者），不静默。"""
    import logging
    plan = _plan([_mk("A", create=["./mod-a/src/main/java/Foo.java"]),
                  _mk("B", create=["mod-a/src/main/java/Foo.java"]),
                  _mk("C", readable=["mod-a/src/main/java/Foo.java"])])
    with caplog.at_level(logging.WARNING):
        added, cyc = wire_readable_provenance(plan)
    assert not added, "多产者歧义文件绝不挂任意产者"
    assert any("G2" in r.message and "漏归一" in r.message for r in caplog.records)


def test_g2_deep_chain_no_recursion_error():
    """复核 MEDIUM：~3000 跳深依赖链不得触发 RecursionError（迭代式 _reaches）。"""
    n = 3000
    subs = [_mk("s0", create=["prod/src/main/java/P.java"])]
    for i in range(1, n):
        subs.append(_mk(f"s{i}", deps=[f"s{i-1}"]))
    # 末端消费者读 s0 的产物，但已通过长链传递依赖 s0 → 不加边，且绝不崩
    subs.append(_mk("consumer", readable=["prod/src/main/java/P.java"], deps=[f"s{n-1}"]))
    plan = _plan(subs)
    added, cyc = wire_readable_provenance(plan)   # 不抛 RecursionError 即通过
    assert ("consumer", "s0") not in added   # 传递已达 s0，不重复加
