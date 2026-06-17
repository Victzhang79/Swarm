"""tech_design 两阶段产出单测（DESIGN 第八节 A+B，ultra 规避单次超长输出卡死）。"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from swarm.brain.planning_nodes import _tech_design_staged


class _Resp:
    def __init__(self, content):
        self.content = content


@pytest.mark.asyncio
async def test_staged_two_phase_merges_modules():
    """两阶段：阶段1 出 2 模块，阶段2 各出文件 → 合并 file_plan。"""
    stage1 = {"architecture": "分层", "data_model": "alarm表", "stack": {},
              "fact_issues": [],
              "modules": [{"name": "alarm-task", "responsibility": "预警任务", "est_files": 2},
                          {"name": "alarm-channel", "responsibility": "渠道", "est_files": 1}]}
    m1 = {"file_plan": [{"path": "a/AlarmTask.java", "action": "create"},
                        {"path": "a/AlarmTaskMapper.java", "action": "create"}]}
    m2 = {"file_plan": [{"path": "c/Channel.java", "action": "create", "module": "alarm-channel"}]}
    llm = AsyncMock()
    llm.ainvoke.side_effect = [
        _Resp(json.dumps(stage1)), _Resp(json.dumps(m1)), _Resp(json.dumps(m2)),
    ]
    result, fp, fi, contract = await _tech_design_staged(
        llm, "建预警平台", "ultra", True, {}, "结构", "无核验", "")
    assert len(fp) == 3, f"应合并 3 文件: {fp}"
    # 阶段2 漏填 module 的应自动补成模块名
    task_fp = [x for x in fp if "AlarmTask" in x["path"]][0]
    assert task_fp["module"] == "alarm-task", "应自动补 module 字段"
    assert llm.ainvoke.await_count == 3, "1 阶段1 + 2 模块 = 3 次调用"


@pytest.mark.asyncio
async def test_staged_no_modules_fallback():
    """阶段1 没给 modules → 退回（不进阶段2，避免空转）。"""
    stage1 = {"architecture": "x", "modules": [], "file_plan": [{"path": "z.java"}]}
    llm = AsyncMock()
    llm.ainvoke.side_effect = [_Resp(json.dumps(stage1))]
    result, fp, fi, contract = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    assert llm.ainvoke.await_count == 1, "无模块不应进阶段2"
    assert fp == [{"path": "z.java"}]


@pytest.mark.asyncio
async def test_staged_module_failure_isolated():
    """某模块阶段2 失败 → 降级跳过，不阻断其他模块。"""
    stage1 = {"architecture": "x", "data_model": "y", "fact_issues": [],
              "modules": [{"name": "m1", "est_files": 1}, {"name": "m2", "est_files": 1}]}
    llm = AsyncMock()
    llm.ainvoke.side_effect = [
        _Resp(json.dumps(stage1)),
        _Resp("garbage not json"),  # m1 解析失败
        _Resp(json.dumps({"file_plan": [{"path": "m2/F.java", "module": "m2"}]})),  # m2 成功
    ]
    result, fp, fi, contract = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    # m1 失败但 m2 成功，至少拿到 m2 的文件（不全军覆没）
    assert any("m2/F.java" in x.get("path", "") for x in fp), f"m2 应成功: {fp}"
