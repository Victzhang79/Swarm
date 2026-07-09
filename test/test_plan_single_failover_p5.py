"""P5（round37b，ECC §G）：单次规划路径"慢→切备用模型" — 行为测试。

定案依据 memory/swarm-e2e-round37-postmortem ④ + docs/ECC_TRANSPLANT_REGISTER §G：
  R35-A 分批路径已具"primary 外层墙钟超时→显式切备用(Kimi)"；单次规划路径此前只有
  llm.with_fallbacks（仅兜流【内】错误，"慢"不切）→ GLM 饱和稳定慢产 >timeout 时干等
  超时降级空 scope 兜底。治本=单发也走 _invoke_llm_abortable：流式+chunk 看门狗+墙钟超时
  显式切备，与分批同构。

栈无关：抽象 plan JSON，无语言/框架词汇。
"""

from __future__ import annotations

import asyncio

from swarm.brain.nodes import plan
from swarm.types import Complexity


class _Resp:
    def __init__(self, content):
        self.content = content


class _TimeoutPrimary:
    """稳定慢产的 primary——ainvoke 抛 asyncio.TimeoutError（模拟墙钟超时）。无 astream
    →_invoke_llm_abortable 走 wait_for 路径，超时后切备用。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise asyncio.TimeoutError()


class _FakeFallback:
    """备用模型救回：返回合法单子任务 plan JSON。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _Resp('{"subtasks":[{"id":"st-1","description":"备用产出",'
                     '"scope":{"writable":["a"],"readable":[]}}],'
                     '"parallel_groups":[["st-1"]]}')


async def test_single_plan_slow_primary_fails_over_to_fallback(monkeypatch):
    """单发规划 primary 墙钟超时 → 切备用模型救回（plan 用备用产出，非空 scope 降级兜底）。"""
    import swarm.brain.nodes as nodes
    primary = _TimeoutPrimary()
    fallback = _FakeFallback()
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: primary)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: fallback)
    out = await plan({
        "task_description": "build a feature",
        "complexity": Complexity.MEDIUM,   # 非 ultra + 无 file_plan → 单发路径
    })
    assert primary.calls == 1 and fallback.calls == 1, "primary 超时后必须切备用一次"
    task_plan = out["plan"]
    assert [s.description for s in task_plan.subtasks] == ["备用产出"], \
        "plan 应来自备用模型救回，而非超时降级空兜底"
    assert out.get("plan_generation_failed") is False, "切备成功不算规划生成失败"


async def test_single_plan_both_timeout_degrades_not_crash(monkeypatch):
    """primary 与备用【双超时】→ 走既有 degraded 空兜底（不炸链），plan_generation_failed=True。"""
    import swarm.brain.nodes as nodes
    primary = _TimeoutPrimary()
    fallback = _TimeoutPrimary()  # 备用也超时
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: primary)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: fallback)
    out = await plan({
        "task_description": "build a feature",
        "complexity": Complexity.MEDIUM,
    })
    assert primary.calls == 1 and fallback.calls == 1
    assert out.get("plan_generation_failed") is True, "双超时→降级兜底并打 fail-fast 标记"
    # 兜底计划仍是合法结构（不炸链）
    assert len(out["plan"].subtasks) >= 1


async def test_single_plan_no_fallback_configured_degrades(monkeypatch):
    """未配置备用（_get_brain_fallback_llm=None）→ primary 超时直接降级（退化原行为，不炸）。"""
    import swarm.brain.nodes as nodes
    primary = _TimeoutPrimary()
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: primary)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    out = await plan({
        "task_description": "build a feature",
        "complexity": Complexity.MEDIUM,
    })
    assert primary.calls == 1
    assert out.get("plan_generation_failed") is True
