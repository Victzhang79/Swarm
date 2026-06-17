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
    assert out == {}


@pytest.mark.asyncio
async def test_contract_design_ultra_multimodule(monkeypatch):
    """ultra 多模块 → 调 Brain 大模型产共享契约，落 shared_contract_draft。"""
    import json as _json
    from unittest.mock import AsyncMock
    import swarm.brain.planning_nodes as pn

    contract = {"interfaces": [{"name": "INotifyService", "module": "channel",
                                "signature": "send(NotifyRequest):NotifyResponse"}],
                "dtos": [], "constants": [], "apis": [], "conventions": []}

    class _Resp:
        content = _json.dumps({"shared_contract": contract})

    fake = AsyncMock()
    fake.ainvoke.return_value = _Resp()
    monkeypatch.setattr(pn, "_get_brain_llm", lambda: fake)

    out = await pn.contract_design({
        "complexity": "ultra",
        "tech_design": {"modules": [{"name": "channel"}, {"name": "engine"}],
                        "data_model": "x"},
        "tech_design_file_plan": [{"path": "channel/INotifyService.java", "module": "channel"}],
        "task_description": "建预警平台",
    })
    assert "shared_contract_draft" in out
    assert out["shared_contract_draft"]["interfaces"][0]["name"] == "INotifyService"
    fake.ainvoke.assert_awaited_once()
