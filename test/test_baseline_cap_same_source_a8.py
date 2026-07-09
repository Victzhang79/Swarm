"""A8（2026-07-09 深读登记册·阶段0）：baseline 申报帽与抽取条目上限同源 — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §二 A8：
  - normalize_baseline_covered 硬帽 100、第 101 条起静默 break；而抽取侧上限
    自适应、硬 backstop=500（P4）。棕地项目底座需求 >100 条时，申报越诚实越完整
    越被静默砍 → 被砍条目回到 uncovered → 覆盖闸死。
  - 治本：两帽同源（申报帽 = requirements_extract._HARD_MAX_ITEMS 单一事实源）。
    申报数结构上不可能超过抽取条目总数，同源后"诚实申报被砍"不再可能；
    超帽（此时必为 LLM 失控吐假 ID）WARNING 留痕不再静默。

栈无关：抽象 req id。
"""

from __future__ import annotations

import logging

from swarm.brain.plan_validator import normalize_baseline_covered
from swarm.brain.requirements_extract import _HARD_MAX_ITEMS


def _decl(i):
    return {"id": f"req-{i:08x}", "reason": f"存量已满足（条目 {i}）"}


def test_honest_declarations_over_100_survive():
    """250 条诚实申报（棕地大底座）→ 全数保留，不再第 101 条静默砍。"""
    raw = [_decl(i) for i in range(250)]
    out = normalize_baseline_covered(raw)
    assert len(out) == 250, (
        f"申报帽必须与抽取上限同源（{_HARD_MAX_ITEMS}），诚实申报被静默砍={len(out)}")


def test_cap_equals_extraction_hard_max():
    """帽 = 抽取硬 backstop（单一事实源）；超帽仍有界截断（防 runaway 膨胀）。"""
    raw = [_decl(i) for i in range(_HARD_MAX_ITEMS + 50)]
    out = normalize_baseline_covered(raw)
    assert len(out) == _HARD_MAX_ITEMS


def test_overflow_leaves_loud_trace(caplog):
    """超帽（结构上必为失控假 ID）→ WARNING 留痕，不静默。"""
    raw = [_decl(i) for i in range(_HARD_MAX_ITEMS + 7)]
    with caplog.at_level(logging.WARNING):
        normalize_baseline_covered(raw)
    assert any("baseline" in r.message and "7" in r.message
               for r in caplog.records), "超帽丢弃必须 WARNING 留痕（含丢弃条数）"


def test_dedupe_and_reason_bound_unchanged():
    """既有语义回归：按 id 去重保优、reason 300 字符有界、垃圾类型丢弃。"""
    out = normalize_baseline_covered([
        {"id": "req-x", "reason": ""},
        {"id": "req-x", "reason": "补上理由"},
        "req-y",
        {"id": "", "reason": "no id"},
        42,
        {"id": "req-z", "reason": "长" * 400},
    ])
    assert [e["id"] for e in out] == ["req-x", "req-y", "req-z"]
    assert out[0]["reason"] == "补上理由"
    assert len(out[2]["reason"]) == 300
