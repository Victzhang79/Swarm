"""治本 B：依赖契约真落 ultra。

根因（f9e38dae ultra 路径）：Rule5 据 shared_contract.dependencies 把【模块依赖并集】
落进 pom owner 子任务的验收，但 tech_design/contract_design 从不产 dependencies → Rule5 空转 →
后续子任务用 RedisTemplate/@Slf4j 但 pom 没声明 → mvn compile 必败 → 全量 replan。

本测覆盖：
1. _normalize_contract_dependencies：把 LLM 各种形态规整成 Rule5 可消费的 [{module,artifacts}]。
2. contract_design：ultra 多模块场景产出的契约含规范化 dependencies。
3. 端到端：contract_design 产 dependencies → Rule5 把依赖落进 pom owner 验收。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.planning_nodes as pn
from swarm.brain.contract_utils import normalize_plan_scopes
from swarm.brain.planning_nodes import _normalize_contract_dependencies
from swarm.types import SubTask, TaskPlan, FileScope


# ── 1. _normalize_contract_dependencies 纯函数 ──

def test_normalize_standard_list():
    raw = [{"module": "ruoyi-alarm/", "artifacts": ["lombok", "lombok", " spring-boot-starter-data-redis "]}]
    out = _normalize_contract_dependencies(raw)
    assert out == [{"module": "ruoyi-alarm", "artifacts": ["lombok", "spring-boot-starter-data-redis"]}]


def test_normalize_dict_form():
    raw = {"ruoyi-alarm": ["lombok"], "ruoyi-notify": ["fastjson2", "hutool-all"]}
    out = _normalize_contract_dependencies(raw)
    assert {"module": "ruoyi-alarm", "artifacts": ["lombok"]} in out
    assert {"module": "ruoyi-notify", "artifacts": ["fastjson2", "hutool-all"]} in out


def test_normalize_drops_empty_artifacts():
    raw = [{"module": "m1", "artifacts": []}, {"module": "", "artifacts": ["x"]}, {"module": "m2", "artifacts": ["y"]}]
    out = _normalize_contract_dependencies(raw)
    assert out == [{"module": "m2", "artifacts": ["y"]}]


def test_normalize_garbage_returns_empty():
    assert _normalize_contract_dependencies(None) == []
    assert _normalize_contract_dependencies("nonsense") == []
    assert _normalize_contract_dependencies([1, 2, 3]) == []


# ── 2. contract_design（三段式）产出含规范化 dependencies ──
# 治本后 contract_design = 骨架 + 逐模块并发 + 确定性合并。dependencies 由【各模块片】产出、
# 在 Stage C 合并时各自归一（容 list/dict 两种形态）。下面用 routing fake LLM：骨架 call 返回
# 空骨架，模块 call 返回该模块的契约片（携带被测的 dependencies 形态）。

class _Resp:
    def __init__(self, content): self.content = content


def _routing_llm(module_slice_json: str):
    """骨架 call → 空骨架；模块 call → 给定契约片（携带被测 dependencies）。"""
    class _L:
        async def ainvoke(self, msgs):
            sys = msgs[0]["content"]
            if "consumer_map" in sys:  # Stage A 骨架
                return _Resp('{"skeleton": {"conventions": [], "constants": [], "consumer_map": []}}')
            return _Resp(module_slice_json)  # Stage B 单模块片
    return lambda: _L()


def _ultra_singlemodule_state():
    # 单模块片测 dependencies 归一：仍需 ≥2 模块才进三段式，故给 2 个同名片由合并去重收口
    return {
        "assessed_complexity": "ultra",
        "task_description": "企业级预警编排平台",
        "tech_design": {
            "modules": [{"name": "ruoyi-alarm", "responsibility": "核心"},
                        {"name": "ruoyi-alarm", "responsibility": "核心"}],
            "data_model": "Alarm{...}",
        },
    }


def test_contract_design_emits_normalized_dependencies():
    # 模块片给 list 形态 + 尾斜杠 + 重复 → 合并归一后去斜杠/去重
    slice_json = ('{"interfaces": [], "dtos": [],'
                  ' "dependencies": [{"module": "ruoyi-alarm/", "artifacts": ["lombok", "lombok"]}]}')
    with patch.object(pn, "_get_brain_llm", _routing_llm(slice_json)):
        out = asyncio.run(pn.contract_design(_ultra_singlemodule_state()))
    deps = out["shared_contract_draft"]["dependencies"]
    assert deps == [{"module": "ruoyi-alarm", "artifacts": ["lombok"]}]


def test_contract_design_dict_form_dependencies_normalized():
    # 模块片给 dict 形态 → 各片归一时容错（治本：_merge 逐片 _normalize）
    slice_json = '{"dependencies": {"ruoyi-notify": ["fastjson2"]}}'
    with patch.object(pn, "_get_brain_llm", _routing_llm(slice_json)):
        out = asyncio.run(pn.contract_design(_ultra_singlemodule_state()))
    assert out["shared_contract_draft"]["dependencies"] == [{"module": "ruoyi-notify", "artifacts": ["fastjson2"]}]


def test_contract_design_missing_dependencies_is_empty_list_not_crash():
    slice_json = '{"interfaces": [], "dtos": []}'
    with patch.object(pn, "_get_brain_llm", _routing_llm(slice_json)):
        out = asyncio.run(pn.contract_design(_ultra_singlemodule_state()))
    assert out["shared_contract_draft"]["dependencies"] == []


# ── 3. 端到端：dependencies → Rule5 落进 pom owner 验收 ──

def _plan_with_pom_owner(deps):
    pom_owner = SubTask(
        id="st-scaffold",
        description="建 ruoyi-alarm 模块脚手架",
        scope=FileScope(create_files=["ruoyi-alarm/pom.xml"], writable=[], readable=[]),
        acceptance_criteria=["mvn -pl ruoyi-alarm compile 通过"],
    )
    engine = SubTask(
        id="st-engine",
        description="告警引擎",
        scope=FileScope(writable=["ruoyi-alarm/src/main/java/Engine.java"], readable=[]),
    )
    return TaskPlan(
        subtasks=[pom_owner, engine],
        shared_contract={"dependencies": deps},
    )


def test_rule5_lands_dependencies_into_pom_owner_acceptance():
    plan = _plan_with_pom_owner([
        {"module": "ruoyi-alarm", "artifacts": ["spring-boot-starter-data-redis", "lombok"]},
    ])
    changed = normalize_plan_scopes(plan)
    assert changed
    owner = next(st for st in plan.subtasks if st.id == "st-scaffold")
    note = " ".join(owner.acceptance_criteria)
    assert "ruoyi-alarm/pom.xml 必须声明依赖" in note
    assert "lombok" in note and "spring-boot-starter-data-redis" in note


# ── A5 治本(round11)：逻辑模块落进单物理模块 → 无独立 owner 的依赖归并到唯一 pom owner ──
def test_a5_ownerless_logical_module_dep_reconciled_to_sole_owner():
    p = TaskPlan(subtasks=[
        SubTask(id="st-1", description="脚手架",
                scope=FileScope(create_files=["ruoyi-alarm/pom.xml", "ruoyi-alarm/src/App.java"])),
        SubTask(id="st-2", description="robot",
                scope=FileScope(create_files=["ruoyi-alarm/src/robot/Robot.java"]), depends_on=["st-1"]),
    ], parallel_groups=[["st-1"]])
    p.shared_contract = {"dependencies": [
        {"module": "alarm-robot", "artifacts": ["com.squareup.okhttp3:okhttp"]}]}
    normalize_plan_scopes(p, None)
    st1 = next(s for s in p.subtasks if s.id == "st-1")
    acs = st1.acceptance_criteria or []
    assert any("okhttp" in c and "alarm-robot" in c for c in acs), \
        "逻辑模块 alarm-robot 无独立 pom → 依赖应归并到唯一物理模块 owner st-1，不落空"


def test_a5_multi_module_ambiguous_does_not_reconcile():
    """多物理模块 pom owner(真多模块)→ 歧义 → 保守不归并(行为不变，只告警)。"""
    p = TaskPlan(subtasks=[
        SubTask(id="st-1", description="mod-a 脚手架", scope=FileScope(create_files=["mod-a/pom.xml"])),
        SubTask(id="st-2", description="mod-b 脚手架", scope=FileScope(create_files=["mod-b/pom.xml"])),
    ], parallel_groups=[["st-1"], ["st-2"]])
    p.shared_contract = {"dependencies": [
        {"module": "ghost-module", "artifacts": ["g:art"]}]}
    normalize_plan_scopes(p, None)
    for s in p.subtasks:
        assert not any("ghost-module" in c or "g:art" in c for c in (s.acceptance_criteria or [])), \
            "多模块歧义时不得擅自把 ownerless 依赖归并到某个模块"


def test_a5_direct_owner_unchanged_regression():
    """契约模块有【直接】pom owner → 原行为：落进该模块 owner，不走归并分支。"""
    p = TaskPlan(subtasks=[
        SubTask(id="st-1", description="alarm-robot 脚手架",
                scope=FileScope(create_files=["alarm-robot/pom.xml"])),
    ], parallel_groups=[["st-1"]])
    p.shared_contract = {"dependencies": [
        {"module": "alarm-robot", "artifacts": ["x:y"]}]}
    normalize_plan_scopes(p, None)
    st1 = p.subtasks[0]
    assert any("alarm-robot/pom.xml 必须声明依赖" in c and "x:y" in c
               for c in (st1.acceptance_criteria or [])), "直接 owner 应走原路径"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
