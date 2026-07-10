"""R38-D（round38 治本 #4）：EXTRACT_REQ 全失败禁止报"完成：0 条"继续走。

round38 实测：3 次 LLM 调用全部被账本拒绝后节点打 INFO"完成：0 条合法条目"继续走——
需求分母被静默清零，PLAN_COVERAGE_GATE 对空 items 整体跳过 = 覆盖闸从根上失去牙；
若任务没死在 PLAN-BATCH，会带 0 需求走到底并"全覆盖"交付（高危 silent-pass）。

治本（区分两类 0 条）：
  - llm_failed（全部尝试异常、源文本非空、从未拿到可解析输出）→ raise fail-loud
    （基础设施/预算问题，不是"PRD 没需求"；覆盖链无分母的交付不是生产级交付）。
  - all_rejected（模型可达、输出全被防幻觉校验拒）→ 保持既有 degraded 降级路径
    （能力 artifact，既有闸门/needs_review 兜，先例#1c 不加修复）。
  - 空源文本 → 保持既有降级（不调 LLM，非 bug）。
  - sibling（R38-C）：循环内账本拒绝 → 等在飞结算再重试；hopeless → 立即放弃。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from swarm.brain.requirements_extract import extract_requirements
from swarm.models.errors import TaskTokenLimitExceeded

_SRC = "系统需要支持批量导入数据文件。提供一个概览页面。" * 20  # 非空需求源


class _StubLLM:
    def __init__(self, contents: list):
        self.contents = list(contents)
        self.n_calls = 0

    async def ainvoke(self, messages):
        self.n_calls += 1
        item = self.contents.pop(0) if self.contents else json.dumps({"items": []})
        if isinstance(item, Exception):
            raise item
        return type("R", (), {"content": item})()


def _wire(monkeypatch, stub):
    import swarm.brain.nodes as nodes_pkg
    monkeypatch.setattr(nodes_pkg, "_get_brain_llm", lambda: stub)


def _good_payload() -> str:
    return json.dumps({"items": [
        {"text": "支持批量导入数据文件", "kind": "功能",
         "source_quote": "系统需要支持批量导入数据文件"},
    ]}, ensure_ascii=False)


def test_all_llm_failures_raise_failloud(monkeypatch):
    """全部尝试异常 + 源非空 → raise（绝不"完成：0 条"继续走）。"""
    _wire(monkeypatch, _StubLLM([RuntimeError("boom")] * 5))
    with pytest.raises(Exception) as ei:
        asyncio.run(extract_requirements({"task_description": _SRC}))
    assert "EXTRACT_REQ" in str(ei.value)


def test_empty_source_still_degrades_without_raise(monkeypatch):
    """空源不调 LLM，保持既有降级路径（行为锁）。"""
    _wire(monkeypatch, _StubLLM([]))
    out = asyncio.run(extract_requirements({"task_description": ""}))
    assert out["requirement_items"] == []
    assert any("empty_source" in r for r in out.get("degraded_reasons", []))


def test_all_rejected_keeps_degraded_path(monkeypatch):
    """模型可达但输出全被防幻觉拒 → 不 raise，保持 degraded（能力 artifact 先例#1c）。"""
    hallucinated = json.dumps({"items": [
        {"text": "无中生有", "kind": "功能", "source_quote": "此句不在源文本中出现XYZ"},
    ]}, ensure_ascii=False)
    _wire(monkeypatch, _StubLLM([hallucinated] * 5))
    out = asyncio.run(extract_requirements({"task_description": _SRC}))
    assert out["requirement_items"] == []
    assert any("requirements_extract:empty" in r for r in out.get("degraded_reasons", []))


def test_recovers_when_later_attempt_succeeds(monkeypatch):
    """首次异常、次轮成功 → 正常产出（不因曾失败 raise）。"""
    _wire(monkeypatch, _StubLLM([RuntimeError("boom"), _good_payload()]))
    out = asyncio.run(extract_requirements({"task_description": _SRC}))
    assert len(out["requirement_items"]) == 1


def test_token_denial_waits_for_admission_then_recovers(monkeypatch):
    """sibling R38-C：账本拒绝 → 等准入（wait→fit）→ 重试成功。"""
    probes = iter(["fit"])
    monkeypatch.setattr(
        "swarm.models.ledger.admission_probe",
        lambda task_id, est, kind="cloud": next(probes))
    _wire(monkeypatch, _StubLLM([
        TaskTokenLimitExceeded({"requested_est": 100, "total": 1}), _good_payload()]))
    out = asyncio.run(extract_requirements(
        {"task_description": _SRC, "task_id": "t-ext"}))
    assert len(out["requirement_items"]) == 1


def test_token_denial_hopeless_gives_up_fast_and_raises(monkeypatch):
    """hopeless → 不空转重试（LLM 只被调 1 次）→ fail-loud raise。"""
    monkeypatch.setattr(
        "swarm.models.ledger.admission_probe",
        lambda task_id, est, kind="cloud": "hopeless")
    stub = _StubLLM([TaskTokenLimitExceeded({"requested_est": 10**9, "total": 1})] * 5)
    _wire(monkeypatch, stub)
    with pytest.raises(Exception):
        asyncio.run(extract_requirements(
            {"task_description": _SRC, "task_id": "t-hop"}))
    assert stub.n_calls == 1
