"""R55-1 治本锁：reasoning runaway → 就地关 thinking 无损重开流。

round55 实锤：EXTRACT_REQ 一次云端调用跑了 1471s / 79605 chunk，**前 400+ chunk 一个正文都没有**
——全是 reasoning。三道既有防线全都拦不住：
  · max_tokens 只封最终答案（reasoning_content 豁免）；
  · stall 双超时只看 chunk 间隔（它一直在吐，看门狗判"健康"）；
  · 墙钟兜底设在 1500s（故意保守，怕误杀合法慢调用）→ 必须先烧满 25 分钟才切备模型、且丢掉全部成果。

关键洞察：**正文尚未吐出一个字之前，中途 abort 是无损的**（下游一个 chunk 都没收到）
→ 超预算即就地关 thinking、用同一模型重开流，下游无感（实测 11s 拿到正文）。
"""
from __future__ import annotations

import asyncio

import pytest


class _Chunk:
    def __init__(self, content: str = ""):
        self.content = content


@pytest.mark.asyncio
async def test_runaway_on_primary_switches_model_not_degrade_thinking(monkeypatch):
    """★R56-1 头号锁★ 链上还有备选 → **抛 transient 切备用大脑**（保留完整推理），绝不降级思考。

    round56 实锤：关 thinking 后同一模型确实产出了、流程也救活了，但**质量掉了**——同一份 PRD，
    有推理抽 106 条需求、关推理只抽 92 条，且**整块功能消失**（AES/SHA512 加密、企微 Bot/Lark/VoIP
    渠道、排班快照 5 个数据字段、3 个 API）。覆盖闸**看不见**这种损失（只校验"抽出来的都被覆盖"）。
    故：能换模型就换模型，别让同一个模型关着脑子硬写。
    """
    from langchain_openai import ChatOpenAI

    from swarm.models.errors import TransientInfraError
    from swarm.models.router import _DualTimeoutChatOpenAI

    async def _fake_astream(self, *args, **kwargs):
        for _ in range(500):
            await asyncio.sleep(0.002)
            yield _Chunk("")          # 思维链空转，永不吐正文

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)
    llm = _DualTimeoutChatOpenAI(
        model="fake", api_key="x", base_url="http://x/v1",
        swarm_reasoning_phase_budget=0.05, swarm_no_fallback=False,   # 链上还有备选
        swarm_first_token_timeout=5, swarm_inter_chunk_timeout=5,
    )
    with pytest.raises(TransientInfraError, match="reasoning runaway"):
        async for _ in llm._astream([]):
            pass


@pytest.mark.asyncio
async def test_chain_tail_degrades_thinking_as_last_resort(monkeypatch):
    """链尾（无备选可切）→ 才退到关 thinking 重开；下游必须仍拿到完整正文（无损，有产出胜过烧穿墙钟）。"""
    from langchain_openai import ChatOpenAI

    from swarm.models.router import _DualTimeoutChatOpenAI

    calls: list[dict] = []

    async def _fake_astream(self, *args, **kwargs):
        calls.append(dict(kwargs.get("extra_body") or {}))
        if len(calls) == 1:
            for _ in range(500):              # 思维链空转：只有空 content 的 chunk
                await asyncio.sleep(0.002)    # 让墙钟真的走起来（预算才可能被烧穿）
                yield _Chunk("")
        else:
            yield _Chunk("最终答案")           # 关 thinking 后立刻出正文

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)

    llm = _DualTimeoutChatOpenAI(
        model="fake", api_key="x", base_url="http://x/v1",
        swarm_reasoning_phase_budget=0.05,    # 极小预算 → 必然触发
        swarm_no_fallback=True,               # ★链尾：没人可切 → 只能降级思考
        swarm_first_token_timeout=5, swarm_inter_chunk_timeout=5,
    )
    out = ""
    async for c in llm._astream([]):
        out += str(getattr(c, "content", "") or "")

    assert out == "最终答案", "★ 降级重开必须无损：下游仍拿到完整正文"
    assert len(calls) == 2, "应且仅应重开一次"
    assert calls[0].get("thinking") is None, "首次调用不动 thinking（保留推理能力）"
    assert calls[1]["thinking"] == {"type": "disabled"}, "★ 重开时必须显式关 thinking"


@pytest.mark.asyncio
async def test_content_started_then_slow_is_never_restarted(monkeypatch):
    """已开始吐正文 → abort 不再无损 → 绝不重开（交给墙钟/stall 兜底）。"""
    from langchain_openai import ChatOpenAI

    from swarm.models.router import _DualTimeoutChatOpenAI

    calls: list[dict] = []

    async def _fake_astream(self, *args, **kwargs):
        calls.append(dict(kwargs.get("extra_body") or {}))
        yield _Chunk("正")                      # 先出正文
        for _ in range(50):                     # 之后再慢吞吞
            await asyncio.sleep(0.001)
            yield _Chunk("")
        yield _Chunk("文")

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)
    llm = _DualTimeoutChatOpenAI(
        model="fake", api_key="x", base_url="http://x/v1",
        swarm_reasoning_phase_budget=0.005, swarm_no_fallback=True,
        swarm_first_token_timeout=5, swarm_inter_chunk_timeout=5,
    )
    out = ""
    async for c in llm._astream([]):
        out += str(getattr(c, "content", "") or "")
    assert out == "正文"
    assert len(calls) == 1, "★ 正文已开吐 → 绝不重开（重开会丢已交付的 chunk）"


def test_worker_chain_tail_is_marked_no_fallback():
    """★R56-1 补齐（worker 面）★ 链尾必须被标 no_fallback，且**只有**链尾。

    猎手实锤：此前只有 brain 的 fallback 被标；worker 链尾未标 → 思考失控时它抛
    TransientInfraError 却**无人可接**（后面没有模型了）→ 整个 worker 调用失败，
    比 R55-1 之前（就地关 thinking 仍能产出）更糟。链序是 breaker 健康**重排后**的，
    因此链尾只能在装配处认定。
    """
    from swarm.models.router import ModelRouter

    class _Fake:
        def __init__(self, name):
            self.name = name
            self.swarm_no_fallback = False

        def with_listeners(self, **_kw):
            return self

        def with_fallbacks(self, others):
            return ("chain", self, others)

    models = [("m1", _Fake("m1")), ("m2", _Fake("m2")), ("m3", _Fake("m3"))]
    ModelRouter._assemble_worker_chain(models)

    assert [m.swarm_no_fallback for _n, m in models] == [False, False, True], \
        "只有链尾可以降级关 thinking；非链尾必须切模型（关 thinking 会漏需求）"
