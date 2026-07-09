"""P4（round37b）：需求抽取上限【随规模自适应】+ NFR 不被优先级贬砍 — 行为测试。

定案依据 memory/swarm-req-extract-over-limit-fixed-threshold + swarm-e2e-round37-postmortem：
  - round37b 实测：108 条【全接地全非重复】的真需求撞固定 MAX_ITEMS=100 砍 8 条，6 条是
    NFR（可用/安全/幂等/可插拔 → kind=other），按 R32-4 kind 优先级最先砍掉 → 漏进验收网。
  - 用户判定：固定绝对阈值把"LLM 失控（真信号=低接地/高重复，已被 quote_not_in_source +
    duplicate 单独抓）"与"PRD 真大"混为一谈，违背"如实还原需求第一"。
  - 治本三点：①阈值随规模自适应（源料规模是尺度）；②失控靠质量签名非计数；
    ③NFR 不被优先级贬砍（截断改到达序 keep-first，kind 中性）。

栈无关：抽象条目，无语言/框架/领域词汇。
"""

from __future__ import annotations

from swarm.brain.requirements_extract import (
    _CHARS_PER_REQ,
    _HARD_MAX_ITEMS,
    _effective_items_limit,
    validate_requirement_items,
)


def _raw(text, kind, quote):
    return {"text": text, "kind": kind, "source_quote": quote}


# ─────────────────── _effective_items_limit 自适应阈值（纯函数）───────────────────

def test_effective_limit_tiny_source_holds_floor(monkeypatch):
    """tiny 源料 → 阈值守住配置下限（不因源短而更严，也不放行 runaway）。"""
    monkeypatch.delenv("SWARM_EXTRACT_MAX_ITEMS", raising=False)
    assert _effective_items_limit("短短的一句需求") == 100


def test_effective_limit_scales_up_with_large_source(monkeypatch):
    """大 PRD → 阈值随源料规模上抬（108 条真需求不再被固定 100 砍）。"""
    monkeypatch.delenv("SWARM_EXTRACT_MAX_ITEMS", raising=False)
    big = "需" * (150 * _CHARS_PER_REQ)  # 规模足以容纳 ~150 条
    assert _effective_items_limit(big) >= 108
    assert _effective_items_limit(big) == 150


def test_effective_limit_capped_by_hard_backstop(monkeypatch):
    """病态巨量源料 → 硬 backstop 封顶（防真爆炸）。"""
    monkeypatch.delenv("SWARM_EXTRACT_MAX_ITEMS", raising=False)
    huge = "需" * (_HARD_MAX_ITEMS * _CHARS_PER_REQ * 3)
    assert _effective_items_limit(huge) == _HARD_MAX_ITEMS


def test_effective_limit_env_floor_wins_over_adaptive(monkeypatch):
    """显式 env 下限高于自适应值时 env 胜（用户覆盖优先）。"""
    monkeypatch.setenv("SWARM_EXTRACT_MAX_ITEMS", "300")
    assert _effective_items_limit("短") == 300


# ─────────────────── 端到端：108 条真需求全过（治 round37b 漏 NFR）───────────────────

def test_108_grounded_nonduplicate_reqs_all_survive():
    """108 条全接地全非重复的真需求（含 NFR）在足量源料下【一条不砍】。"""
    # 每条独立 quote 且都在源料中（接地）、文本各异（非重复）。真实大 PRD：足量源料
    # （≥108×_CHARS_PER_REQ）才谈得上表达 108 条需求——自适应阈值据此上抬容纳。
    pad = "补" * _CHARS_PER_REQ  # 每条需求背后约 _CHARS_PER_REQ 源字符（贴近真实 PRD 密度）
    quotes = [f"条目描述编号{i:03d}{pad}" for i in range(108)]
    source = "。".join(quotes) + "。"
    raw = [_raw(f"系统需求{i:03d}", "other" if i % 3 == 0 else "functional", quotes[i])
           for i in range(108)]
    items, rejected = validate_requirement_items(raw, source)
    assert len(items) == 108, "全接地非重复真需求不得被固定阈值砍"
    assert not [r for r in rejected if r["reason"] == "over_limit"]
    # NFR（kind=other）没有被系统性砍掉
    assert sum(1 for it in items if it["kind"] == "other") == 36


def test_over_limit_truncates_arrival_order_kind_neutral(monkeypatch):
    """超自适应阈值时按到达序 keep-first 截断，kind 中性——NFR/other 不再最先砍。"""
    monkeypatch.setenv("SWARM_EXTRACT_MAX_ITEMS", "3")
    src = ("系统需要页面乙。系统需要功能甲。系统需要其他戊。"
           "系统需要接口丙。系统需要数据丁。")
    raw = [
        _raw("页面乙条目", "page", "系统需要页面乙"),
        _raw("功能甲条目", "functional", "系统需要功能甲"),
        _raw("其他戊条目", "other", "系统需要其他戊"),   # NFR：到达序第3，必须保留
        _raw("接口丙条目", "api", "系统需要接口丙"),
        _raw("数据丁条目", "data", "系统需要数据丁"),
    ]
    items, rejected = validate_requirement_items(raw, src)
    texts = [i["text"] for i in items]
    assert texts == ["页面乙条目", "功能甲条目", "其他戊条目"], "到达序 keep-first，NFR 不被贬砍"
    dropped = {r["text_head"] for r in rejected if r["reason"] == "over_limit"}
    assert dropped == {"接口丙条目", "数据丁条目"}


def test_within_adaptive_limit_zero_behavior_change(monkeypatch):
    """未超（自适应）阈值 → 保留全部 + 到达序不变（零行为变化）。"""
    monkeypatch.delenv("SWARM_EXTRACT_MAX_ITEMS", raising=False)
    src = "系统需要功能甲。系统需要页面乙。"
    raw = [_raw("功能甲条目", "functional", "系统需要功能甲"),
           _raw("页面乙条目", "page", "系统需要页面乙")]
    items, rejected = validate_requirement_items(raw, src)
    assert [i["text"] for i in items] == ["功能甲条目", "页面乙条目"]
    assert not [r for r in rejected if r["reason"] == "over_limit"]
