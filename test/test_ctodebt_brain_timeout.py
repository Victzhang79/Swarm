"""FINDING-10(task 25a6d83c)：Brain 规划调用失控/挂起防护。

现场：PLAN-BATCH 拆到批 9/11 后，brain 模型(GLM-5.2 云端 reasoning)对批 10 的调用【失控持续
生成】挂 16.5min——无 chunk 看门狗抓不到(chunk 一直在吐)、read-timeout 不管总时长 → 整个 PLAN
无限挂。两道防护：① brain 调用设 max_tokens 上限(截断失控生成)；② PLAN-BATCH 每批 LLM 调用加
asyncio.wait_for 总墙钟上限(与 TECH_DESIGN stage2 同构)，超时按已有 except 分支降级跳过。
"""
from __future__ import annotations

import inspect

from swarm.config.settings import ModelConfig
from swarm.models.router import ModelRouter


def test_brain_has_max_tokens_cap():
    """brain 默认有输出上限(非 0)——防 reasoning 模型失控持续生成。"""
    c = ModelConfig()
    assert c.brain_max_tokens and c.brain_max_tokens > 0, "brain 应有 max_tokens 上限"


def test_get_brain_llm_wires_max_tokens():
    """get_brain_llm 把 brain_max_tokens 传进 get_chat_model(primary + fallback 都传)。"""
    src = inspect.getsource(ModelRouter.get_brain_llm)
    assert "max_tokens" in src and "brain_max_tokens" in src
    # 构建不崩
    assert ModelRouter(ModelConfig()).get_brain_llm() is not None


def test_get_chat_model_sets_max_tokens_attr():
    """get_chat_model 传入的 max_tokens 真落到底层 ChatOpenAI。"""
    r = ModelRouter(ModelConfig())
    prov = r._get_provider_for_model(ModelConfig().brain_primary)
    m = prov.get_chat_model("some-model", max_tokens=32768)
    val = getattr(m, "max_tokens", None) or getattr(m, "max_completion_tokens", None)
    assert val == 32768, f"max_tokens 应落到底层模型，实际 {val}"


def test_plan_batch_has_wall_clock_timeout():
    """PLAN-BATCH 每批 LLM 调用必须有 asyncio.wait_for 总墙钟上限，否则失控生成无限挂死 PLAN。"""
    import swarm.brain.nodes as _n
    src = open(_n.__file__, encoding="utf-8").read()
    assert "_PLAN_BATCH_TIMEOUT" in src, "PLAN-BATCH 应定义总时长上限常量"
    assert "PLAN_BATCH_SYSTEM" in src
    # R34-1 G3 后墙钟收口在 _invoke_llm_abortable（流式=deadline 看门狗；无 astream
    # 回退 wait_for(ainvoke, total_timeout)）——守卫意图不变：批调用必有总墙钟上限。
    assert "_invoke_llm_abortable(" in src, "PLAN-BATCH 的 LLM 调用应走带墙钟的 helper"
    assert "_PLAN_BATCH_TIMEOUT," in src or "timeout=_PLAN_BATCH_TIMEOUT" in src, \
        "批调用必须传 _PLAN_BATCH_TIMEOUT 总时长上限"
    assert "wait_for(" in src, "无流式回退路径应保留 asyncio.wait_for 包裹"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
