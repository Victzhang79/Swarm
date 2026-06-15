"""回归：ELABORATE 悬空依赖兜底（Bug-1，task 0f93f1fc 实证）。

二次拆分 + 多轮 replan 后，下游子任务 depends_on 可能残留指向不存在子任务的旧 id，
_remap_dependents 只兜单次 resplit 映射，导致 VALIDATE_PLAN 结构校验 "依赖未知任务"
死循环。_prune_dangling_dependencies 是 plan 成型后的单一收口点。
"""

from __future__ import annotations

from swarm.brain.planning_nodes import _prune_dangling_dependencies
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality


def _mk(sid: str, deps: list[str]) -> SubTask:
    return SubTask(
        id=sid,
        description=f"sub {sid}",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[], readable=[]),
        depends_on=deps,
        acceptance_criteria=["ok"],
    )


def test_dangling_remapped_to_child_chain_tail():
    # st-2 依赖旧 st-1（已被拆成 st-1-1/st-1-2）→ 应重映射到尾节点 st-1-2
    subs = [_mk("st-1-1", []), _mk("st-1-2", ["st-1-1"]), _mk("st-2", ["st-1"])]
    fixed = _prune_dangling_dependencies(subs)
    assert fixed == 1
    st2 = next(s for s in subs if s.id == "st-2")
    assert st2.depends_on == ["st-1-2"], st2.depends_on


def test_dangling_no_prefix_match_stripped():
    # st-2 依赖一个完全不存在、也无前缀子链的 id → 剥离
    subs = [_mk("st-1", []), _mk("st-2", ["ghost-99"])]
    fixed = _prune_dangling_dependencies(subs)
    assert fixed == 1
    st2 = next(s for s in subs if s.id == "st-2")
    assert st2.depends_on == [], st2.depends_on


def test_valid_deps_unchanged():
    # 全部依赖都存在 → 不改动
    subs = [_mk("st-1", []), _mk("st-2", ["st-1"])]
    fixed = _prune_dangling_dependencies(subs)
    assert fixed == 0
    assert next(s for s in subs if s.id == "st-2").depends_on == ["st-1"]


def test_dedup_and_no_self_dep():
    # 重映射后可能与已有 dep 重复 / 指向自己 → 去重 + 去自指
    subs = [
        _mk("st-1-1", []),
        _mk("st-1-2", ["st-1-1"]),
        _mk("st-2", ["st-1", "st-1-2"]),  # st-1→st-1-2，与已有 st-1-2 重复
    ]
    fixed = _prune_dangling_dependencies(subs)
    assert fixed == 1
    st2 = next(s for s in subs if s.id == "st-2")
    assert st2.depends_on == ["st-1-2"], st2.depends_on  # 去重后只剩一个


def test_mixed_valid_and_dangling():
    subs = [
        _mk("st-1-1", []),
        _mk("st-1-2", ["st-1-1"]),
        _mk("st-3", []),
        _mk("st-2", ["st-1", "st-3", "ghost"]),  # st-1→尾, st-3保留, ghost剥离
    ]
    _prune_dangling_dependencies(subs)
    st2 = next(s for s in subs if s.id == "st-2")
    assert set(st2.depends_on) == {"st-1-2", "st-3"}, st2.depends_on


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== 悬空依赖兜底: {len(fns)}/{len(fns)} passed ===")
