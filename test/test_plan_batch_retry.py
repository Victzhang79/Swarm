#!/usr/bin/env python3
"""P6a：plan-batch 模块分解失败【重试】（996db614 实测 2/9 模块批失败→零子任务→交付残缺）。

批分解 timeout/error/空 此前无重试静默丢。失败多为 GLM-5.2 瞬时 timeout，加 1 次重试大概率恢复。
本测：首次 TimeoutError → 重试成功 → 该模块子任务齐（非被丢）。
"""
from __future__ import annotations

import asyncio

import pytest

import swarm.brain.nodes as nodes


class _Resp:
    def __init__(self, content):
        self.content = content


def _flaky_llm(fail_times: int, subtasks_json: str, calls: dict):
    class _L:
        async def ainvoke(self, msgs):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise asyncio.TimeoutError()
            return _Resp(subtasks_json)
    return _L()


def _state():
    return {"tech_design": {"data_model": "Alarm{}", "modules": [{"name": "modA"}]},
            "shared_contract_draft": {}}


def _file_plan():
    return [{"path": "modA/src/main/java/com/x/A.java", "module": "modA",
             "action": "create", "responsibility": "核心"}]


@pytest.mark.asyncio
async def test_plan_batch_retries_on_timeout():
    calls = {"n": 0}
    subs = ('{"subtasks":[{"id":"st-1","description":"建 A",'
            '"scope":{"writable":["modA/src/main/java/com/x/A.java"]},'
            '"acceptance_criteria":["mvn -pl modA compile"]}]}')
    llm = _flaky_llm(1, subs, calls)  # 第 1 次 timeout，第 2 次成功
    plan = await nodes._plan_ultra_batched(
        llm, _state(), "建预警平台", "ultra", {}, "", "", "", "", _file_plan(),
    )
    assert calls["n"] == 2, f"应重试(旧代码 1 次即丢),实际 {calls['n']}"
    # 模块子任务被抢救（非降级成单空 fallback 子任务）
    paths = [p for st in plan.subtasks for p in (getattr(st.scope, "writable", []) or [])]
    assert any("A.java" in p for p in paths), f"重试成功该模块子任务应在: {plan.subtasks}"


@pytest.mark.asyncio
async def test_plan_batch_exhausts_then_drops():
    calls = {"n": 0}
    llm = _flaky_llm(99, "{}", calls)  # 永远 timeout
    # round27 F5：全部批次失败必须【抛出】，由 plan() 的 except 映射为 plan_generation_failed
    # 降级（can_auto_accept_plan fail-fast 拦下）。旧契约"静默返回单 fallback 空 scope 子任务"
    # 绕过该标记 → auto_accept 下空计划被放行 → worker 无文件可写假失败。
    with pytest.raises(RuntimeError):
        await nodes._plan_ultra_batched(
            llm, _state(), "建预警平台", "ultra", {}, "", "", "", "", _file_plan(),
        )
    assert calls["n"] == nodes_plan_batch_attempts(), f"应有界重试,实际 {calls['n']}"


def nodes_plan_batch_attempts():
    import os
    return int(os.environ.get("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "2") or "2")


if __name__ == "__main__":
    import sys
    asyncio.run(test_plan_batch_retries_on_timeout())
    asyncio.run(test_plan_batch_exhausts_then_drops())
    print("OK")
    sys.exit(0)
