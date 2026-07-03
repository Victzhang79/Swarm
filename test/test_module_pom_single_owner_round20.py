"""#11(a) 复现+治本：模块 pom 也须【单写者】，非串行两写者。

根因（round19 实测，merged_diff 双 <project> 拼接）：D1 只把【根 pom】收敛为单写者
（_is_root_pom）。模块 pom（ruoyi-alarm/pom.xml，有目录前缀）两个写者若在【同一串行链】上
（st-3 依赖 st-1-1），normalize_plan_scopes 让二者都保留写权 → MERGE union/3-way 把两份
【整段 pom 全文件】拼接 → 畸形双 <project> 根 → apply 后 pom 不可解析 → 交付死于门口。

治本：任何 pom（根/模块）都是结构性全文件，两个写者各自整段重写 <modules>/<dependencies>
无法安全合并 → 一律单写者，非首写者 demote+依赖 owner（与 D1 根 pom 同理）。
"""

from __future__ import annotations

from swarm.brain.contract_utils import normalize_plan_scopes
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _st(sid, writable=None, create=None, readable=None, depends=None):
    return SubTask(
        id=sid, description="x", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [], create_files=create or [], readable=readable or []),
        depends_on=depends or [], contract={},
    )


def _owners(plan, f):
    return [s.id for s in plan.subtasks
            if f in (list(s.scope.create_files) + list(s.scope.writable))]


def test_module_pom_single_owner_even_same_chain():
    """两个写者【同串行链】(st-3 依赖 st-1-1) 也不能双写模块 pom → 唯一 owner=st-1-1。"""
    plan = TaskPlan(subtasks=[
        _st("st-1-1", create=["ruoyi-alarm/pom.xml"]),
        _st("st-3", create=["ruoyi-alarm/pom.xml"], depends=["st-1-1"]),
    ])
    normalize_plan_scopes(plan)
    assert _owners(plan, "ruoyi-alarm/pom.xml") == ["st-1-1"]
    s3 = next(s for s in plan.subtasks if s.id == "st-3")
    assert "ruoyi-alarm/pom.xml" in s3.scope.readable
    assert "st-1-1" in s3.depends_on


def test_module_pom_single_owner_independent_writers():
    """两个【独立】写者(无依赖)抢建模块 pom → 唯一 owner + 非首写者串行到 owner。"""
    plan = TaskPlan(subtasks=[
        _st("st-1-1", create=["ruoyi-alarm/pom.xml"]),
        _st("st-9", create=["ruoyi-alarm/pom.xml"]),
    ])
    normalize_plan_scopes(plan)
    assert _owners(plan, "ruoyi-alarm/pom.xml") == ["st-1-1"]
    s9 = next(s for s in plan.subtasks if s.id == "st-9")
    assert "st-1-1" in s9.depends_on


def test_distinct_module_poms_keep_own_owner():
    """不同模块的 pom 各自独立 owner，绝不误收敛。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/pom.xml"]),
        _st("st-2", create=["ruoyi-alarm-sdk/pom.xml"]),
    ])
    normalize_plan_scopes(plan)
    assert _owners(plan, "ruoyi-alarm/pom.xml") == ["st-1"]
    assert _owners(plan, "ruoyi-alarm-sdk/pom.xml") == ["st-2"]


def test_module_pom_owner_keeps_write():
    """单一写者不受影响（无争用不 demote）。"""
    plan = TaskPlan(subtasks=[
        _st("st-1-1", create=["ruoyi-alarm/pom.xml"]),
        _st("st-2", create=["ruoyi-alarm/src/main/java/A.java"]),
    ])
    normalize_plan_scopes(plan)
    assert _owners(plan, "ruoyi-alarm/pom.xml") == ["st-1-1"]
