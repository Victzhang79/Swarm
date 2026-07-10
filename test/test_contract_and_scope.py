"""T1 共享契约 contract_design 节点 + T3 同文件写权唯一 单测。"""
import pytest

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _st(sid, writable=None, create=None, readable=None, depends=None):
    return SubTask(
        id=sid, description="x", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [], create_files=create or [], readable=readable or []),
        depends_on=depends or [], contract={},
    )


# ── T3：同文件写权唯一 ──
def test_t3_normalize_dedupe_write():
    """同一文件被两个子任务列为写目标 → 保留首个写者，后者降级 readable。"""
    from swarm.brain.contract_utils import normalize_plan_scopes
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["a/Foo.java"]),
        _st("st-2", writable=["a/Foo.java"]),  # 同文件抢写
    ])
    changed = normalize_plan_scopes(plan)
    assert changed, "应检测到同文件多写者并归一"
    # st-1 保留写权
    s1 = next(s for s in plan.subtasks if s.id == "st-1")
    assert "a/Foo.java" in (s1.scope.create_files + s1.scope.writable)
    # st-2 的写权被降级（不再在 writable/create）
    s2 = next(s for s in plan.subtasks if s.id == "st-2")
    assert "a/Foo.java" not in s2.scope.writable
    assert "a/Foo.java" not in s2.scope.create_files


def test_t3_no_change_when_disjoint():
    """不同文件无交集 → 不改动。"""
    from swarm.brain.contract_utils import normalize_plan_scopes
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["a/Foo.java"]),
        _st("st-2", create=["b/Bar.java"]),
    ])
    normalize_plan_scopes(plan)
    s1 = next(s for s in plan.subtasks if s.id == "st-1")
    s2 = next(s for s in plan.subtasks if s.id == "st-2")
    assert "a/Foo.java" in s1.scope.create_files
    assert "b/Bar.java" in s2.scope.create_files


# ── T1：contract_design 节点 ──
@pytest.mark.asyncio
async def test_contract_design_skips_non_ultra():
    """非 ultra / 单模块 → 直通空（不浪费 Brain 调用）。"""
    from swarm.brain.planning_nodes import contract_design
    out = await contract_design({
        "complexity": "medium", "tech_design": {"modules": [{"name": "m1"}]},
        "tech_design_file_plan": [],
    })
    # C4-8 语义演进（round38c 主题C）：非 ultra 早退 always-emit 清空机读键防跨轮粘滞
    assert out == {"contract_failed_modules": []}


@pytest.mark.asyncio
async def test_contract_design_ultra_multimodule(monkeypatch):
    """ultra 多模块 → 三段式（骨架+逐模块并发+合并）产共享契约，落 shared_contract_draft。"""
    import swarm.brain.planning_nodes as pn

    class _Resp:
        def __init__(self, content): self.content = content

    class _RoutingLLM:
        async def ainvoke(self, msgs):
            if "consumer_map" in msgs[0]["content"]:  # Stage A 骨架
                return _Resp('{"skeleton": {"conventions": [], "constants": [], "consumer_map": []}}')
            # Stage B：channel 模块吐 INotifyService，其余空片
            mod = next((ln.split("：", 1)[1].strip() for ln in msgs[1]["content"].splitlines()
                        if ln.startswith("模块名：")), "?")
            if mod == "channel":
                return _Resp('{"interfaces": [{"name": "INotifyService", "module": "channel",'
                             ' "signature": "send(NotifyRequest):NotifyResponse"}],'
                             ' "dtos": [], "apis": [], "dependencies": []}')
            return _Resp('{"interfaces": [], "dtos": [], "apis": [], "dependencies": []}')

    monkeypatch.setattr(pn, "_get_brain_llm", lambda: _RoutingLLM())

    out = await pn.contract_design({
        "complexity": "ultra",
        "tech_design": {"modules": [{"name": "channel", "responsibility": "渠道"},
                                    {"name": "engine", "responsibility": "引擎"}],
                        "data_model": "x"},
        "task_description": "建预警平台",
    })
    assert "shared_contract_draft" in out
    names = [i["name"] for i in out["shared_contract_draft"]["interfaces"]]
    assert "INotifyService" in names
