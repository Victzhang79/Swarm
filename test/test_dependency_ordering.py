"""治本 RUN17(依赖倒置死锁)回归：脚手架置根 + SQL 依赖实体 + SQL 不挡路。

RUN17 死锁现场：decompose 把"建全部表 DDL"(st-1,无依赖)放成全局根,
st-2(SQL seed)→st-3(脚手架)→所有功能 全吊在它后面。st-1 无实体上下文 900s 空转 →
整个项目卡死(28 个子任务一个没动)。
"""

from __future__ import annotations

from swarm.brain.contract_utils import (
    _is_scaffold_subtask,
    _is_sql_subtask,
    dedupe_module_scaffolds,
    fix_dependency_ordering,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _mk(sid, *, create=None, deps=None):
    return SubTask(
        id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=create or []), depends_on=deps or [],
        acceptance_criteria=["ok"],
    )


def _run17_inverted_plan():
    j = "ruoyi-alarm/src/main/java/com/ruoyi/alarm"
    return TaskPlan(subtasks=[
        _mk("st-1", create=["sql/alarm/alarm_schema.sql"], deps=[]),            # SQL 巨任务(误置根)
        _mk("st-2", create=["sql/alarm/menu_seed.sql"], deps=["st-1"]),         # SQL seed
        _mk("st-3", create=["ruoyi-alarm/pom.xml"], deps=["st-2"]),             # 脚手架(被吊后面)
        _mk("st-4", create=[f"{j}/domain/AlarmBot.java", f"{j}/controller/AlarmBotController.java"],
            deps=["st-3"]),                                                     # 功能(java)
        _mk("st-6", create=[f"{j}/domain/AlarmApp.java"], deps=["st-3"]),
    ], parallel_groups=[], shared_contract={})


def test_classifiers():
    p = _run17_inverted_plan()
    by = {s.id: s for s in p.subtasks}
    assert _is_sql_subtask(by["st-1"]) and _is_sql_subtask(by["st-2"])
    assert _is_scaffold_subtask(by["st-3"])
    assert not _is_sql_subtask(by["st-4"]) and not _is_scaffold_subtask(by["st-4"])


def test_scaffold_becomes_root():
    p = _run17_inverted_plan()
    assert fix_dependency_ordering(p) is True
    st3 = next(s for s in p.subtasks if s.id == "st-3")
    assert st3.depends_on == [], f"脚手架应置根,实得 {st3.depends_on}"


def test_nobody_depends_on_sql():
    p = _run17_inverted_plan()
    fix_dependency_ordering(p)
    for s in p.subtasks:
        if s.id in ("st-1", "st-2"):
            continue
        assert "st-1" not in s.depends_on and "st-2" not in s.depends_on, \
            f"{s.id} 不应依赖 SQL,实得 {s.depends_on}"


def test_sql_depends_on_entities_and_runs_last():
    p = _run17_inverted_plan()
    fix_dependency_ordering(p)
    java_ids = {"st-4", "st-6"}
    for sid in ("st-1", "st-2"):
        s = next(x for x in p.subtasks if x.id == sid)
        assert set(s.depends_on) == java_ids, f"{sid} 应依赖所有实体子任务,实得 {s.depends_on}"


def test_sql_reads_entity_files():
    p = _run17_inverted_plan()
    fix_dependency_ordering(p)
    st1 = next(s for s in p.subtasks if s.id == "st-1")
    assert any("AlarmBot.java" in f for f in st1.scope.readable), \
        f"SQL 子任务应读到实体 domain 文件(照字段建表),实得 {st1.scope.readable}"


def test_no_cycle_after_fix():
    """修正后无环：scaffold→java→sql 单向。"""
    p = _run17_inverted_plan()
    fix_dependency_ordering(p)
    idx = {s.id: s for s in p.subtasks}

    def reaches(a, b, seen=None):
        seen = seen or set()
        for d in idx[a].depends_on or []:
            if d == b or (d not in seen and reaches(d, b, seen | {d})):
                return True
        return False
    for s in p.subtasks:
        assert not reaches(s.id, s.id), f"{s.id} 自依赖成环"


def test_noop_when_no_sql_or_scaffold():
    p = TaskPlan(subtasks=[_mk("st-1", create=["a/X.java"]), _mk("st-2", create=["a/Y.java"], deps=["st-1"])],
                 parallel_groups=[], shared_contract={})
    assert fix_dependency_ordering(p) is False


def _run17_dup_scaffold_plan():
    """RUN17 现场：4 个子任务都建 ruoyi-alarm/pom.xml（重复地基,VALIDATE 判严重却没修）。"""
    j = "ruoyi-alarm/src/main/java/com/ruoyi/alarm"
    return TaskPlan(subtasks=[
        _mk("st-3", create=["ruoyi-alarm/pom.xml", f"{j}/core/AlarmConstants.java"]),
        _mk("st-12", create=["ruoyi-alarm/pom.xml", f"{j}/config/AlarmConfig.java"], deps=["st-3"]),
        _mk("st-27", create=["ruoyi-alarm/pom.xml"], deps=["st-3"]),
        _mk("st-34", create=["ruoyi-alarm/pom.xml"]),
        _mk("st-4", create=[f"{j}/domain/AlarmBot.java"], deps=["st-12"]),   # 下游依赖被合并者
        # 另一个不同模块的脚手架,不应被合并
        _mk("st-32", create=["alarm-api/pom.xml"]),
    ], parallel_groups=[], shared_contract={})


def test_dedupe_merges_duplicate_module_scaffolds():
    p = _run17_dup_scaffold_plan()
    merged = dedupe_module_scaffolds(p)
    assert merged == 3, f"4 个重复 ruoyi-alarm 脚手架应合并掉 3 个,实得 {merged}"
    ids = {s.id for s in p.subtasks}
    # 保留首个 st-3,删除 st-12/27/34
    assert "st-3" in ids and not ({"st-12", "st-27", "st-34"} & ids)
    # 不同模块 alarm-api 脚手架保留
    assert "st-32" in ids


def test_dedupe_unions_create_files_into_canonical():
    p = _run17_dup_scaffold_plan()
    dedupe_module_scaffolds(p)
    st3 = next(s for s in p.subtasks if s.id == "st-3")
    cf = st3.scope.create_files
    assert any("AlarmConstants" in f for f in cf) and any("AlarmConfig" in f for f in cf), \
        f"被合并者的 create 应并入 canonical,实得 {cf}"


def test_dedupe_remaps_dependents_to_canonical():
    p = _run17_dup_scaffold_plan()
    dedupe_module_scaffolds(p)
    st4 = next(s for s in p.subtasks if s.id == "st-4")
    assert st4.depends_on == ["st-3"], f"依赖被合并者 st-12 应重映射到 canonical st-3,实得 {st4.depends_on}"


def test_dedupe_no_self_dep():
    p = _run17_dup_scaffold_plan()
    dedupe_module_scaffolds(p)
    for s in p.subtasks:
        assert s.id not in (s.depends_on or []), f"{s.id} 不应自依赖"


def test_dedupe_noop_single_scaffold():
    p = TaskPlan(subtasks=[
        _mk("st-1", create=["ruoyi-alarm/pom.xml"]),
        _mk("st-2", create=["a/X.java"], deps=["st-1"]),
    ], parallel_groups=[], shared_contract={})
    assert dedupe_module_scaffolds(p) == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ✅ {fn.__name__}")
    print(f"\n=== 依赖序修正(脚手架置根+SQL依赖实体): {len(fns)}/{len(fns)} passed ===")
