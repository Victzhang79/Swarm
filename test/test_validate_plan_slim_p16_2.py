#!/usr/bin/env python3
"""P16-2 治本单测 —— VALIDATE_PLAN 软校验 plan_json 瘦身，防 1MB prompt 拖推理模型 runaway。

round16 实测：`plan_obj.model_dump_json()` 把每子任务约 42K 的 contract 副本(24 子任务重复
24×) + 注入代码全序列化 → plan_json ~1MB(~260K token)，喂给推理模型 GLM-5.2 触发 84K chunk /
25min reasoning runaway(撞 1500s wall-clock 上限才放行、结果软建议还被丢弃)→ 卡在到 DISPATCH 前。

固化：slim 剥离每子任务 contract/context_snippets，保留结构字段(id/deps/scope/描述)+plan 级
shared_contract；对 1MB 级 plan 瘦身后应远小于 LLM 上限。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_validator import (
    MAX_LLM_VALIDATION_PLAN_CHARS,
    slim_plan_json_for_llm_validation,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan

# 单子任务约 42K 字符的 contract 副本（还原 round16 实测体量）
_BIG_CONTRACT = {f"iface_{i}": {"methods": ["m()" ] * 40, "blob": "x" * 400} for i in range(90)}


def _sub(sid, deps=None):
    return SubTask(
        id=sid, description=f"task {sid} 的功能描述",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[f"{sid}/A.java"], readable=[f"{sid}/B.java"]),
        depends_on=deps or [],
        contract=dict(_BIG_CONTRACT),               # 重体积冗余
        context_snippets="// 注入代码\n" + "y" * 2000,  # 注入代码
    )


def _bloated_plan(n=24):
    subs = [_sub(f"st-{i}", deps=[f"st-{i-1}"] if i > 1 else []) for i in range(1, n + 1)]
    return TaskPlan(subtasks=subs, shared_contract={"iface_0": {"methods": ["m()"]}})


def test_slim_strips_contract_and_snippets():
    plan = _bloated_plan(24)
    slim = slim_plan_json_for_llm_validation(plan)
    data = json.loads(slim)
    for st in data["subtasks"]:
        assert "contract" not in st, "contract 应被剥离"
        assert "context_snippets" not in st, "context_snippets 应被剥离"
    print("  ✅ 每子任务 contract/context_snippets 已剥离")


def test_slim_preserves_structural_fields():
    plan = _bloated_plan(5)
    data = json.loads(slim_plan_json_for_llm_validation(plan))
    ids = [st["id"] for st in data["subtasks"]]
    assert ids == [f"st-{i}" for i in range(1, 6)], ids
    st3 = next(s for s in data["subtasks"] if s["id"] == "st-3")
    assert st3["depends_on"] == ["st-2"], "depends_on 应保留(DAG 校验需要)"
    assert st3["scope"]["writable"] == ["st-3/A.java"], "scope 应保留(写冲突校验需要)"
    assert st3["description"].startswith("task st-3"), "description 应保留"
    # plan 级 shared_contract 保留一次（契约完整性由它体现）
    assert "iface_0" in data["shared_contract"], "plan 级 shared_contract 应保留"
    print("  ✅ id/depends_on/scope/description/shared_contract 保留")


def test_slim_drastically_smaller_than_raw():
    plan = _bloated_plan(24)
    raw = plan.model_dump_json(indent=2)
    slim = slim_plan_json_for_llm_validation(plan)
    assert len(raw) > 800_000, f"还原的 bloated raw 应 ~1MB，实际 {len(raw)}"
    assert len(slim) < len(raw) // 10, f"slim 应 <10% raw：raw={len(raw)} slim={len(slim)}"
    assert len(slim) < MAX_LLM_VALIDATION_PLAN_CHARS, \
        f"slim 应在 LLM 上限 {MAX_LLM_VALIDATION_PLAN_CHARS} 内，实际 {len(slim)}"
    print(f"  ✅ raw={len(raw)} → slim={len(slim)}（<10% 且在 {MAX_LLM_VALIDATION_PLAN_CHARS} 上限内）")


def test_size_guard_constant_sane():
    # 上限须显著小于会触发 runaway 的量级(1MB)，且足够表达结构(>数万字符)
    assert 30_000 < MAX_LLM_VALIDATION_PLAN_CHARS < 300_000
    print(f"  ✅ MAX_LLM_VALIDATION_PLAN_CHARS={MAX_LLM_VALIDATION_PLAN_CHARS} 合理")


if __name__ == "__main__":
    test_slim_strips_contract_and_snippets()
    test_slim_preserves_structural_fields()
    test_slim_drastically_smaller_than_raw()
    test_size_guard_constant_sane()
    print("\n✅ P16-2 全部通过")
