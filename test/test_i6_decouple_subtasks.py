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


def test_r62_scaffold_ordering_edge_never_stripped():
    """R62-1 治本（round62 死因·结构性边不可剥）：module 脚手架 depends_on 聚合父脚手架的边，
    零文件重叠 + 无契约（脚手架只建 pom，天然零重叠），但它是 **Maven reactor 磁盘落地顺序**
    约束——绝不能被 decouple 当假依赖剥掉。

    round62 现场（task 01520400）：inject 正确造了 `st-scaffold-alarm-channel →
    st-scaffold-ruoyi-alarm` 等 4 条边，decouple 用"文件重叠+契约"启发式（对 reactor 顺序天然盲）
    把它们全剥了 → module 脚手架空 depends_on → 与聚合父同一并行 wave 派发 →
    module_registered_before_scaffold（25 次）→ 连坐放弃 → 完成 11→3。
    判据=**目标 id 以 `st-scaffold-` 开头**（LLM 永不产出此类 id，必为确定性注入器的结构性产物）。
    """
    agg = _st("st-scaffold-ruoyi-alarm", create=["ruoyi-alarm/pom.xml"])
    # module 脚手架：只建自己的 pom（与聚合父零文件重叠、无 contract），depends_on 聚合父
    channel = _st("st-scaffold-alarm-channel", create=["ruoyi-alarm/alarm-channel/pom.xml"],
                  depends_on=["st-scaffold-ruoyi-alarm"])
    engine = _st("st-scaffold-alarm-engine", create=["ruoyi-alarm/alarm-engine/pom.xml"],
                 depends_on=["st-scaffold-ruoyi-alarm"])
    # 写代码子任务：depends_on 本模块脚手架（先有 pom 再编译），同样零文件重叠
    impl = _st("st-impl-1", create=["ruoyi-alarm/alarm-channel/src/X.java"],
               depends_on=["st-scaffold-alarm-channel"])
    plan = _plan([agg, channel, engine, impl])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 0, f"结构性脚手架排序边被误剥 {removed} 条 = round62 死因复活"
    by_id = {st.id: st for st in plan.subtasks}
    assert by_id["st-scaffold-alarm-channel"].depends_on == ["st-scaffold-ruoyi-alarm"]
    assert by_id["st-scaffold-alarm-engine"].depends_on == ["st-scaffold-ruoyi-alarm"]
    assert by_id["st-impl-1"].depends_on == ["st-scaffold-alarm-channel"]
    print("  ✅ 脚手架排序边（module→聚合父 / impl→module）全部保留")


def test_r62_llm_owned_pom_edge_preserved_r58_3():
    """R62 收编（对抗复核 HIGH）：判据用【结构性 _is_scaffold_subtask】而非 id 前缀——覆盖
    R58-3「有 owner≠有模板」：LLM 认领某 module pom.xml 的子任务【结构上是脚手架但无
    st-scaffold- id】。code 子任务 depends_on 该 LLM-pom-owner 同样是真构建序（先有 pom 再编译），
    零文件重叠（pom↔java），旧 id-前缀 guard 漏保 → 被 decouple 误剥（=round62 机制经 R58-3 通道）。
    """
    # LLM 认领的 pom owner——注意 id 是普通 st-N，非 st-scaffold-
    owner = _st("st-7", create=["modx/pom.xml"])
    code = _st("st-8", create=["modx/src/main/java/Svc.java"], depends_on=["st-7"])
    plan = _plan([owner, code])
    removed = _decouple_independent_subtasks(plan)
    assert removed == 0, "LLM 认领 pom 者（结构性脚手架、无 st-scaffold- id）的入边被误剥"
    assert plan.subtasks[1].depends_on == ["st-7"]
    print("  ✅ R58-3 LLM 认领 pom 者的入边保留（结构判据 > id 前缀）")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
