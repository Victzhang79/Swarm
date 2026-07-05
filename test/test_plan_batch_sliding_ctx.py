"""P0-1（CODEWALK_AUDIT_2026-07-06 批1）：ULTRA 分批拆解静默丢弃 sliding_ctx → replan 盲重规划。

原 bug：_plan_ultra_batched 签名收 sliding_ctx（plan() 已把上轮失败根因 replan_feedback
拼在其头部），函数体从未使用 → ULTRA + file_plan>30 的分批路径上 replan 反馈被静默丢弃，
LLM 每轮重生成同样的坏计划（confirm 路径 3rd-P1a 修过的同类问题，分批路径漏修）。
修复：PLAN_BATCH_USER 增 {sliding_context} 占位符，_plan_ultra_batched 把 sliding_ctx
注入每批 user prompt；同时删除 complexity/routing_table/knowledge_prompt/
recent_tasks_prompt 四个从未使用的死参（消除"看起来已接线"的误导）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import swarm.brain.nodes as nodes
from swarm.types import Complexity

_SUBS_JSON = (
    '{"subtasks":[{"id":"st-1","description":"建 A",'
    '"scope":{"writable":["modA/src/main/java/com/x/A.java"]},'
    '"acceptance_criteria":["mvn -pl modA compile"]}]}'
)


class _Resp:
    def __init__(self, content):
        self.content = content


class _CaptureLLM:
    """记录每次 ainvoke 收到的 messages，返回固定合法子任务 JSON。"""

    def __init__(self):
        self.prompts: list[list[dict]] = []

    async def ainvoke(self, msgs):
        self.prompts.append(msgs)
        return _Resp(_SUBS_JSON)


def _file_plan(n: int = 1) -> list[dict]:
    return [
        {"path": f"modA/src/main/java/com/x/F{i}.java", "module": "modA",
         "action": "create", "responsibility": "核心"}
        for i in range(n)
    ]


def _state() -> dict:
    return {"tech_design": {"modules": [{"name": "modA"}]}, "shared_contract_draft": {}}


def test_batch_prompt_carries_sliding_ctx():
    """直连：sliding_ctx 文本必须出现在分批 user prompt 里。"""
    llm = _CaptureLLM()
    feedback = "上轮失败根因：st-3 依赖悬空、scope 与 st-5 冲突（勿重复同样拆分）"
    asyncio.run(nodes._plan_ultra_batched(
        llm, _state(), "建预警平台", {}, feedback, _file_plan(),
    ))
    assert llm.prompts, "应发起分批 LLM 调用"
    user = llm.prompts[0][-1]["content"]
    assert feedback in user, "sliding_ctx（含 replan 失败根因）必须注入每批 user prompt"


def test_every_batch_gets_sliding_ctx():
    """多模块多批时，每一批都要带上下文（不是只有第一批）。"""
    llm = _CaptureLLM()
    fp = _file_plan(2)
    fp[1]["path"] = "modB/src/main/java/com/y/G.java"
    fp[1]["module"] = "modB"
    state = {"tech_design": {"modules": [{"name": "modA"}, {"name": "modB"}]},
             "shared_contract_draft": {}}
    feedback = "上轮失败根因：modB 子任务 scope 越界"
    asyncio.run(nodes._plan_ultra_batched(
        llm, state, "建预警平台", {}, feedback, fp,
    ))
    assert len(llm.prompts) >= 2, "两个模块应各一批"
    for i, msgs in enumerate(llm.prompts):
        assert feedback in msgs[-1]["content"], f"第 {i + 1} 批 prompt 缺 sliding_ctx"


def test_template_drift_degrades_not_crashes(monkeypatch):
    """hunter 1c：模板占位符与传参漂移（未来改 PLAN_BATCH_USER 漏改 format）必须按
    "批失败"降级——全批失败走既有 RuntimeError 降级路径，绝不让 KeyError 裸穿 gather
    被外层 except 伪装成普通 LLM 失败。"""
    import swarm.brain.prompts as prompts

    llm = _CaptureLLM()
    monkeypatch.setattr(
        prompts, "PLAN_BATCH_USER",
        prompts.PLAN_BATCH_USER + "\n{nonexistent_placeholder}",
    )
    with pytest.raises(RuntimeError):
        asyncio.run(nodes._plan_ultra_batched(
            llm, _state(), "任务", {}, "", _file_plan(),
        ))
    assert not llm.prompts, "模板坏是确定性 bug，不应发起任何 LLM 调用"


def test_plan_node_wires_replan_feedback_into_batches():
    """wiring：plan() ULTRA + file_plan>30 分批路径，replan_feedback 须传抵批 prompt。"""
    llm = _CaptureLLM()

    class _Router:
        def get_routing_table(self):
            return {}

    state = {
        "task_description": "建预警平台",
        "complexity": Complexity.ULTRA,
        "assessed_complexity": Complexity.ULTRA,
        "knowledge_context": {},
        "tech_design": {"modules": [{"name": "modA"}]},
        "shared_contract_draft": {},
        "tech_design_file_plan": _file_plan(31),  # >30 触发分批
        "replan_feedback": "上轮 merge 冲突：modA 两个子任务同写 A.java",
        "replan_count": 1,
    }
    with patch.object(nodes, "_get_brain_llm", lambda: llm), \
         patch.object(nodes, "ModelRouter", _Router):
        asyncio.run(nodes.plan(state))
    assert llm.prompts, "ULTRA + 31 文件应走分批路径"
    joined = "\n".join(m["content"] for p in llm.prompts for m in p)
    assert "上轮 merge 冲突" in joined, \
        "replan_feedback 须经 sliding_ctx 注入分批 prompt（否则 replan 退化为盲重规划）"
