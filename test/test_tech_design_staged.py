"""tech_design 两阶段产出单测（DESIGN 第八节 A+B，ultra 规避单次超长输出卡死）。"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from swarm.brain.planning_nodes import _tech_design_staged


class _Resp:
    def __init__(self, content):
        self.content = content


class _RoutedLLM:
    """按提示词内容路由的 mock（R65-T1 后 stage2 是分批协议且各模块并发，
    按位置的 side_effect 在并发+续批下是竞态断言，改为内容路由）。"""

    def __init__(self, stage1: dict, scripts: dict[str, list]):
        self.stage1 = stage1
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.await_count = 0

    async def ainvoke(self, messages):
        self.await_count += 1
        user = messages[-1]["content"]
        if "顶层方案" in messages[0]["content"]:
            return _Resp(json.dumps(self.stage1))
        for name, queue in self.scripts.items():
            if f"模块名：{name}" in user:
                item = queue.pop(0) if queue else {"file_plan": []}
                return _Resp(item if isinstance(item, str) else json.dumps(item))
        raise AssertionError(f"unrouted: {user[:120]}")


@pytest.mark.asyncio
async def test_staged_two_phase_merges_modules():
    """两阶段：阶段1 出 2 模块，阶段2 各出文件 → 合并 file_plan。

    R65-T1 起 stage2 为分批续写协议：每模块产出批后有一次空批确认收敛，
    故调用数 = 1 阶段1 + 每模块(产出批数+1)。"""
    stage1 = {"architecture": "分层", "data_model": "alarm表", "stack": {},
              "fact_issues": [],
              "modules": [{"name": "alarm-task", "responsibility": "预警任务", "est_files": 2},
                          {"name": "alarm-channel", "responsibility": "渠道", "est_files": 1}]}
    m1 = {"file_plan": [{"path": "a/AlarmTask.java", "action": "create"},
                        {"path": "a/AlarmTaskMapper.java", "action": "create"}]}
    m2 = {"file_plan": [{"path": "c/Channel.java", "action": "create", "module": "alarm-channel"}]}
    llm = _RoutedLLM(stage1, {"alarm-task": [m1], "alarm-channel": [m2]})
    result, fp, fi, contract = await _tech_design_staged(
        llm, "建预警平台", "ultra", True, {}, "结构", "无核验", "")
    assert len(fp) == 3, f"应合并 3 文件: {fp}"
    # 阶段2 漏填 module 的应自动补成模块名
    task_fp = [x for x in fp if "AlarmTask" in x["path"]][0]
    assert task_fp["module"] == "alarm-task", "应自动补 module 字段"
    assert llm.await_count == 5, "1 阶段1 + 2 模块×(1 产出批+1 空批确认) = 5 次调用"


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
    llm = _RoutedLLM(stage1, {
        # m1 三次全 garbage（失败预算烧尽）；m2 一批成功 + 空批确认
        "m1": ["garbage not json", "garbage not json", "garbage not json"],
        "m2": [{"file_plan": [{"path": "m2/F.java", "module": "m2"}]}],
    })
    result, fp, fi, contract = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    # m1 失败但 m2 成功，至少拿到 m2 的文件（不全军覆没）
    assert any("m2/F.java" in x.get("path", "") for x in fp), f"m2 应成功: {fp}"
    assert [m["name"] for m in result.get("stage2_failed_modules") or []] == ["m1"]


class _TokenDenied(Exception):
    """模拟账本拒绝（带 usage，走 _await_token_admission 通道）。"""

    def __init__(self):
        super().__init__("token denied")
        self.usage = {"required": 1}


class _RaisingLLM(_RoutedLLM):
    """m1 的调用直接抛账本拒绝异常（不返回响应）。"""

    async def ainvoke(self, messages):
        user = messages[-1]["content"]
        if "模块名：m1" in user:
            self.await_count += 1
            raise _TokenDenied()
        return await super().ainvoke(messages)


@pytest.mark.asyncio
async def test_staged_unhandled_escape_does_not_kill_siblings():
    """R65-F8：单模块协程内逃逸出 try 的未预期异常（实证通道=token 拒绝后
    _await_token_admission 自身故障，该调用在 except 块之外）绝不连坐兄弟模块——
    gather 必须隔离异常，健康模块产出保住，故障模块走 stage2_failed_modules 对账。"""
    stage1 = {"architecture": "x", "data_model": "y", "fact_issues": [],
              "modules": [{"name": "m1", "est_files": 1}, {"name": "m2", "est_files": 1}]}
    llm = _RaisingLLM(stage1, {
        "m2": [{"file_plan": [{"path": "m2/F.java", "module": "m2"}]}],
    })
    with patch("swarm.brain.planning_nodes._is_token_limit_error",
               side_effect=lambda e: isinstance(e, _TokenDenied)), \
         patch("swarm.brain.planning_nodes._await_token_admission",
               AsyncMock(side_effect=RuntimeError("ledger backend down"))):
        result, fp, fi, contract = await _tech_design_staged(
            llm, "需求", "ultra", False, {}, "", "", "")
    assert any("m2/F.java" in x.get("path", "") for x in fp), \
        f"m2 健康产出被 m1 逃逸异常连坐丢失: {fp}"
    _failed = {m["name"]: m for m in result.get("stage2_failed_modules") or []}
    assert "m1" in _failed, f"m1 应走失败对账: {_failed}"
    assert "unhandled" in str(_failed["m1"].get("reason", "")), \
        f"失败原因应机读标注 unhandled: {_failed['m1']}"
