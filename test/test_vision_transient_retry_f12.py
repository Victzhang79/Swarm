"""F12（2026-07-09 深读登记册·阶段0）：vision_ingest 瞬时错误有界重试 — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §七 F12（处方已取证更正）：
  - 病理：_invoke_vision/_ainvoke_vision 用 get_model_by_name 裸模型零重试——
    provider 一次连接抖动/5xx 即整附件理解丢失（上层收敛为 result.error 降级，
    PRD 图片/扫描件内容静默缺失）。
  - 登记册原处方"换 get_llm_by_name"经取证不可用：其 fallback 链是【文本难度】链，
    文本模型看不见图片，fallback 会臆造描述——比 loud 失败更糟（虚假前提进 PRD）。
  - 治本：同模型对 transient 错误（classify_failure=TRANSIENT，复用 B8 中文补齐）
    有界退避重试；capability 类错误（图太大/模型拒答）立刻抛出不烧重试。

栈无关：假 LLM 抽象 provider 行为。
"""

from __future__ import annotations

import asyncio

import swarm.brain.vision_ingest as vi


class _Resp:
    def __init__(self, content):
        self.content = content


class _FlakyLLM:
    """第一次抛 transient，第二次成功。"""

    def __init__(self):
        self.calls = 0

    def invoke(self, msgs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Connection error: peer reset")
        return _Resp("图中是一张系统架构图")

    async def ainvoke(self, msgs):
        return self.invoke(msgs)


class _CapabilityFailLLM:
    def __init__(self):
        self.calls = 0

    def invoke(self, msgs):
        self.calls += 1
        raise RuntimeError("maximum context length exceeded")

    async def ainvoke(self, msgs):
        return self.invoke(msgs)


class _FakeRouter:
    def __init__(self, llm):
        self._llm = llm

    def get_model_by_name(self, name, temperature=0.2):
        return self._llm


def _patch_router(monkeypatch, llm):
    import swarm.models.router as router_mod
    monkeypatch.setattr(router_mod, "ModelRouter", lambda: _FakeRouter(llm))
    monkeypatch.setattr(vi, "_RETRY_BACKOFF_BASE", 0.01, raising=False)


def test_sync_vision_retries_transient(monkeypatch):
    llm = _FlakyLLM()
    _patch_router(monkeypatch, llm)
    out = vi._invoke_vision("m-vision", ["data:image/png;base64,AAAA"])
    assert out == "图中是一张系统架构图"
    assert llm.calls == 2, "transient 抖动必须同模型有界重试，不得一击即丢整附件"


def test_async_vision_retries_transient(monkeypatch):
    llm = _FlakyLLM()
    _patch_router(monkeypatch, llm)
    out = asyncio.run(vi._ainvoke_vision("m-vision", ["data:image/png;base64,AAAA"]))
    assert out == "图中是一张系统架构图"
    assert llm.calls == 2


def test_capability_error_fails_fast_no_retry(monkeypatch):
    """capability 类错误（上下文超限/拒答）重试必然同败——立刻抛出不烧重试。"""
    llm = _CapabilityFailLLM()
    _patch_router(monkeypatch, llm)
    try:
        vi._invoke_vision("m-vision", ["data:image/png;base64,AAAA"])
        raise AssertionError("capability 错误必须抛出（上层降级 result.error）")
    except RuntimeError:
        pass
    assert llm.calls == 1, "capability 错误不得重试"
