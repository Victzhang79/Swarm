"""I6 单测：_decouple_independent_subtasks 剥离假 depends_on（提升并行度）。

判定假依赖需同时满足：零文件重叠 + 无契约耦合 + 都非 allow_any。
真依赖（文件重叠/契约耦合/allow_any）一律保留。纯内存对象，无存储依赖。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.planning_nodes import _decouple_independent_subtasks
from swarm.types import FileScope, SubTask, TaskPlan


def _st(sid, *, writable=None, readable=None, create=None, contract=None, depends_on=None, allow_any=False):
    return SubTask(
        id=sid,
        description=sid,
        scope=FileScope(
            writable=writable or [],
            readable=readable or [],
            create_files=create or [],
            allow_any=allow_any,
        ),
        contract=contract or {},
        depends_on=depends_on or [],
    )


def _plan(subtasks):
    return TaskPlan(subtasks=subtasks)


def test_fake_dep_stripped():
    """零文件重叠 + 无契约 → 假依赖被剥离。"""
    a = _st("a", create=["utils.py"])
    b = _st("b", create=["service.py"], depends_on=["a"])  # b 不碰 utils.py
    plan = _plan([a, b])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 1
    assert plan.subtasks[1].depends_on == []
    print("  ✅ 假依赖被剥离")


def test_real_dep_file_overlap_kept():
    """文件重叠 → 真依赖，保留。"""
    a = _st("a", writable=["models.py"])
    b = _st("b", writable=["models.py"], depends_on=["a"])  # b 改 a 改过的文件
    plan = _plan([a, b])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 0
    assert plan.subtasks[1].depends_on == ["a"]
    print("  ✅ 文件重叠依赖保留")


def test_real_dep_readable_overlap_kept():
    """当前任务 readable 依赖任务写的文件 → 真依赖（要读它的产出），保留。"""
    a = _st("a", create=["api.py"])
    b = _st("b", create=["client.py"], readable=["api.py"], depends_on=["a"])
    plan = _plan([a, b])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 0
    print("  ✅ readable 重叠依赖保留")


def test_contract_coupling_kept():
    """双方都有 contract → 可能契约耦合，保守保留。"""
    a = _st("a", create=["x.py"], contract={"iface": "Foo"})
    b = _st("b", create=["y.py"], contract={"uses": "Foo"}, depends_on=["a"])
    plan = _plan([a, b])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 0
    print("  ✅ 契约耦合依赖保留")


def test_allow_any_kept():
    """allow_any 边界不可判定 → 保留依赖。"""
    a = _st("a", create=["x.py"])
    b = _st("b", allow_any=True, depends_on=["a"])
    plan = _plan([a, b])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 0
    print("  ✅ allow_any 保留依赖")


def test_dangling_dep_kept():
    """悬空依赖 ID（指向不存在的子任务）→ 不臆断，保留。"""
    b = _st("b", create=["y.py"], depends_on=["nonexistent"])
    plan = _plan([b])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 0
    assert plan.subtasks[0].depends_on == ["nonexistent"]
    print("  ✅ 悬空依赖保留")


def test_mixed_partial_strip():
    """一个任务多依赖：真假混合，只剥假的。"""
    a = _st("a", writable=["shared.py"])
    b = _st("b", create=["b.py"])
    c = _st("c", writable=["shared.py"], depends_on=["a", "b"])  # 依赖a(重叠,真) + b(无关,假)
    plan = _plan([a, b, c])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 1
    assert plan.subtasks[2].depends_on == ["a"]  # 保留真依赖 a，剥离假依赖 b
    print("  ✅ 真假混合只剥假依赖")


def test_no_deps_noop():
    """无依赖 → 不动。"""
    a = _st("a", create=["x.py"])
    plan = _plan([a])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 0
    print("  ✅ 无依赖 no-op")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
