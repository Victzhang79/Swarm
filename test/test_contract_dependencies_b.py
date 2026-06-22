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


# ── 2. contract_design 产出含规范化 dependencies ──

class _Resp:
    def __init__(self, content): self.content = content


def _llm_returning(payload_json):
    class _L:
        async def ainvoke(self, _msgs):
            return _Resp(payload_json)
    return lambda: _L()


def _ultra_multimodule_state():
    return {
        "assessed_complexity": "ultra",
        "task_description": "企业级预警编排平台",
        "tech_design": {"modules": ["ruoyi-alarm", "ruoyi-notify"], "data_model": "Alarm{...}"},
        "tech_design_file_plan": [{"path": "ruoyi-alarm/pom.xml", "module": "ruoyi-alarm"}],
    }


def test_contract_design_emits_normalized_dependencies():
    payload = (
        '{"shared_contract": {"interfaces": [], "dtos": [],'
        ' "dependencies": [{"module": "ruoyi-alarm/", "artifacts": ["lombok", "lombok"]}]}}'
    )
    with patch.object(pn, "_get_brain_llm", _llm_returning(payload)):
        out = asyncio.run(pn.contract_design(_ultra_multimodule_state()))
    deps = out["shared_contract_draft"]["dependencies"]
    assert deps == [{"module": "ruoyi-alarm", "artifacts": ["lombok"]}]


def test_contract_design_dict_form_dependencies_normalized():
    payload = '{"shared_contract": {"dependencies": {"ruoyi-notify": ["fastjson2"]}}}'
    with patch.object(pn, "_get_brain_llm", _llm_returning(payload)):
        out = asyncio.run(pn.contract_design(_ultra_multimodule_state()))
    assert out["shared_contract_draft"]["dependencies"] == [{"module": "ruoyi-notify", "artifacts": ["fastjson2"]}]


def test_contract_design_missing_dependencies_is_empty_list_not_crash():
    payload = '{"shared_contract": {"interfaces": [], "dtos": []}}'
    with patch.object(pn, "_get_brain_llm", _llm_returning(payload)):
        out = asyncio.run(pn.contract_design(_ultra_multimodule_state()))
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


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
