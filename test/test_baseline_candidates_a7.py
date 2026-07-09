"""阶段3.5 A7（登记册 §二）：确定性 baseline 候选通道——kb 索引→候选申报清单注入 PLAN。

病理：覆盖闸唯一存量依据=语义检索 top-12 文件按字母序截 25，kb_file_index/
kb_symbol_index 从不喂覆盖闸→棕地底座需求结构上无申报出口（round37 实证 16 条
RuoYi 底座需求 baseline_covered 全程 0）。
治本：纯函数确定性匹配（token→符号/类/文件名打分）产出候选清单注入 PLAN；
reason 必须指向清单中可对账文件；匹配不到=零候选（绝不臆造诱导推卸型假申报）。
"""

from __future__ import annotations

import pytest

from swarm.brain.baseline_candidates import (
    baseline_candidates_prompt_block,
    build_baseline_candidates,
    extract_req_tokens,
)

_FILES = [
    {"file_path": "ruoyi-system/src/SysUserController.java", "module_name": "ruoyi-system",
     "language": "java"},
    {"file_path": "ruoyi-common/src/CaptchaService.java", "module_name": "ruoyi-common",
     "language": "java"},
]
_SYMBOLS = [
    {"file_path": "ruoyi-system/src/SysUserController.java", "symbol_name": "resetPwd",
     "symbol_type": "method", "class_name": "SysUserController"},
    {"file_path": "ruoyi-common/src/CaptchaService.java", "symbol_name": "createCaptcha",
     "symbol_type": "method", "class_name": "CaptchaService"},
]


def test_extract_tokens_identifiers_only():
    toks = extract_req_tokens("系统登录页须支持 CaptchaService 图形验证码并集成 2FA 校验")
    assert "captchaservice" in toks
    assert all(t.isascii() for t in toks), "只抽 ASCII 标识符（中文靠术语对齐）"


def test_candidates_matched_by_symbol_and_class():
    items = [
        {"id": "req-aaaa1111", "text": "沿用现有 CaptchaService 图形验证码能力"},
        {"id": "req-bbbb2222", "text": "用户管理支持重置密码（resetPwd 接口已有）"},
        {"id": "req-cccc3333", "text": "全新的告警订阅推送模块"},  # 无存量
    ]
    cands = build_baseline_candidates(items, _FILES, _SYMBOLS)
    by_id = {c["id"]: c for c in cands}
    assert "req-aaaa1111" in by_id
    assert any("CaptchaService.java" in d["file"]
               for d in by_id["req-aaaa1111"]["candidates"])
    assert "req-bbbb2222" in by_id
    assert "req-cccc3333" not in by_id, "检索不到=零候选（绝不臆造，防推卸型假申报）"


def test_pure_cjk_requirement_yields_no_candidate():
    cands = build_baseline_candidates(
        [{"id": "req-dddd4444", "text": "系统应当支持岗位管理的增删改查"}],
        _FILES, _SYMBOLS)
    assert cands == [], "纯中文无标识符条目宁缺毋滥"


def test_empty_inventory_yields_empty():
    assert build_baseline_candidates(
        [{"id": "req-aaaa1111", "text": "CaptchaService"}], [], []) == []


def test_total_cap_bounded():
    items = [{"id": f"req-{i:08d}", "text": "CaptchaService 能力"} for i in range(300)]
    cands = build_baseline_candidates(items, _FILES, _SYMBOLS, max_total=50)
    assert len(cands) == 50


def test_prompt_block_discipline_and_refs():
    cands = build_baseline_candidates(
        [{"id": "req-aaaa1111", "text": "沿用 CaptchaService 验证码"}], _FILES, _SYMBOLS)
    blk = baseline_candidates_prompt_block(cands)
    assert "req-aaaa1111" in blk and "CaptchaService.java" in blk
    assert "baseline_covered" in blk and "清单外" in blk, "纪律：只许申报清单内+reason 指向可对账文件"
    assert baseline_candidates_prompt_block([]) == ""


# ─────────────── plan 侧接线（fail-open + 注入）───────────────

async def test_block_helper_failopen_without_project(monkeypatch):
    from swarm.brain.nodes import _baseline_candidates_block_for
    assert await _baseline_candidates_block_for({"requirement_items": [{"id": "r"}]}) == ""
    assert await _baseline_candidates_block_for({"project_id": "p"}) == ""


async def test_block_helper_uses_inventory(monkeypatch):
    import swarm.knowledge.service as ksvc

    async def _fake_inventory(pid, *a, **k):
        assert pid == "proj-1"
        return _FILES, _SYMBOLS

    monkeypatch.setattr(ksvc, "fetch_structure_inventory", _fake_inventory)
    from swarm.brain.nodes import _baseline_candidates_block_for
    blk = await _baseline_candidates_block_for({
        "project_id": "proj-1",
        "requirement_items": [{"id": "req-aaaa1111", "text": "沿用 CaptchaService 验证码"}],
    })
    assert "req-aaaa1111" in blk and "存量候选对账清单" in blk


async def test_block_helper_failopen_on_exception(monkeypatch):
    import swarm.knowledge.service as ksvc

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ksvc, "fetch_structure_inventory", _boom)
    from swarm.brain.nodes import _baseline_candidates_block_for
    blk = await _baseline_candidates_block_for({
        "project_id": "proj-1",
        "requirement_items": [{"id": "req-aaaa1111", "text": "CaptchaService"}],
    })
    assert blk == "", "索引异常必须 fail-open 零注入（advisory 绝不拖垮规划）"
