"""回归：VALIDATE_PLAN LLM 验证降级为软建议（Bug-2，task 92ff8a71 等实证）。

过去 fail-closed：LLM 没返回 valid:true 即否决，叠加流式超时返回畸形 JSON →
反复否决耗尽重试 → 主流程卡死在 PLAN。新策略：结构校验通过即放行，LLM 仅软建议。
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

from swarm.brain.nodes import validate_plan
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _plan():
    st = SubTask(
        id="st-1",
        description="新增工具方法",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["a.py"], readable=[]),
        depends_on=[],
        acceptance_criteria=["编译通过"],
    )
    return TaskPlan(subtasks=[st], parallel_groups=[], shared_contract={})


def _state():
    return {
        "plan": _plan(),
        "task_description": "新增工具方法",
        "plan_retry_count": 0,
        "affected_files": [],
        "complexity": "medium",
        "assessed_complexity": "medium",
    }


class _Resp:
    def __init__(self, content):
        self.content = content


def test_llm_says_invalid_but_soft_gate_passes():
    """LLM 返回 valid:false，软建议模式（默认）下结构已过仍 plan_valid=True。"""
    os.environ.pop("SWARM_VALIDATE_PLAN_LLM_GATE", None)  # 默认软建议
    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(return_value=_Resp('{"valid": false, "issues": ["粒度偏大"]}'))
    with patch("swarm.brain.nodes._get_brain_llm", return_value=fake_llm):
        out = asyncio.run(validate_plan(_state()))
    assert out["plan_valid"] is True, "软建议模式下 LLM valid=false 不应阻断"


def test_llm_malformed_json_passes():
    """LLM 返回截断/畸形 JSON（流超时典型）→ 结构已过则放行。"""
    os.environ.pop("SWARM_VALIDATE_PLAN_LLM_GATE", None)
    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(return_value=_Resp('{"valid": tr'))  # 截断
    with patch("swarm.brain.nodes._get_brain_llm", return_value=fake_llm):
        out = asyncio.run(validate_plan(_state()))
    assert out["plan_valid"] is True, "畸形 JSON 不应卡死规划"


def test_hard_gate_env_restores_old_behavior():
    """SWARM_VALIDATE_PLAN_LLM_GATE=true 恢复硬否决。"""
    os.environ["SWARM_VALIDATE_PLAN_LLM_GATE"] = "true"
    try:
        fake_llm = AsyncMock()
        fake_llm.ainvoke = AsyncMock(return_value=_Resp('{"valid": false, "issues": ["x"]}'))
        with patch("swarm.brain.nodes._get_brain_llm", return_value=fake_llm):
            out = asyncio.run(validate_plan(_state()))
        assert out["plan_valid"] is False, "硬门模式下 valid=false 应否决"
    finally:
        os.environ.pop("SWARM_VALIDATE_PLAN_LLM_GATE", None)


def test_llm_valid_true_passes():
    os.environ.pop("SWARM_VALIDATE_PLAN_LLM_GATE", None)
    fake_llm = AsyncMock()
    fake_llm.ainvoke = AsyncMock(return_value=_Resp('{"valid": true, "issues": []}'))
    with patch("swarm.brain.nodes._get_brain_llm", return_value=fake_llm):
        out = asyncio.run(validate_plan(_state()))
    assert out["plan_valid"] is True


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== VALIDATE_PLAN 软建议: {len(fns)}/{len(fns)} passed ===")
