"""★32 号文 S1★ AUDIT 意图劫持链源头（_infer_intent 分类器）— 行为锁。

劫持链：子串匹配把「实现 X 并通过安全审计」整任务翻成 AUDIT
  → dispatch 走 _run_security_audit 短路（只产报告不产 diff）
  → _subtask_produced_expected 对 AUDIT 恒 True（空 diff="符合预期"）
  → verify _all_audit 放行 ⇒ 功能零交付仍判 DONE。
治本：审计词与【建造/修复词】共现时不许翻转 AUDIT（审计词降级为验收语境）；
纯审计意图翻转必须 WARNING 留痕（机读键 intent_audit_inferred）。
注意方向性：误判 MODIFY（多产一份报告文件）远优于误判 AUDIT（静默不交付）。
"""

from __future__ import annotations

import pytest

from swarm.brain.nodes import shared as _shared
from swarm.brain.nodes.shared import _infer_intent
from swarm.types import TaskIntent

# ───────────── 建造/修复词共现 → 绝不翻转 AUDIT ─────────────

@pytest.mark.parametrize("desc", [
    "实现用户登录功能并通过安全审计",
    "开发一个订单模块，要求没有漏洞",
    "新建一个网关服务并通过安全扫描",
    "修复漏洞并补齐单测",
    "implement login and pass security audit",
    "create a new billing service and run sast",
    "fix the cve in dependency parsing",
    # ★双复核 F1：兄弟意图家族（REFACTOR/DEBUG/变更动词）语形——漏掉则劫持链整族活着
    "重构鉴权模块并通过安全审计",
    "优化查询性能并通过安全扫描",
    "改造配置加载逻辑，确保没有漏洞",
    "迁移数据库脚本并通过安全扫描",
    "升级加密组件并通过渗透测试",
    "refactor the parser and pass sast checks",
    "debug the auth flow and run security audit",
    "rewrite the auth module and run a security audit",
    # ★双复核 R2 F1 残留：手抄词表只转一半，以下 7 条实测照旧 STILL-AUDIT——
    # 结构解法=守卫与兄弟分支共用模块级常量，本组钉死复核逮出的具体语形
    "复现线上故障并通过安全扫描",
    "分析 traceback 并跑一遍安全审计",
    "处理 stack trace 里报的 NPE 并过 sast",
    "让 failing test 变绿并通过安全审计",
    "拆分模块并通过安全扫描",
    "整理代码并通过安全审计",
    "代码清理并通过安全扫描",
])
def test_audit_keyword_with_build_words_never_flips_audit(desc):
    intent = _infer_intent(desc)
    assert intent != TaskIntent.AUDIT, (
        f"建造/修复/改造词共现时审计词只是验收语境，翻转 AUDIT=劫持链第一环: {desc!r}")


# ───────────── 漂移锁：守卫词表与兄弟分支共用常量（F1 残留结构解法）─────────────
# 兄弟分支日后加词 ⇒ 守卫自动跟随 ⇒ 本锁自动多钉一格；从守卫删掉任一词 ⇒ 本锁红。

@pytest.mark.parametrize("word", sorted(set(
    _shared._INTENT_DEBUG_KEYWORDS + _shared._INTENT_REFACTOR_KEYWORDS
    + _shared._INTENT_CREATE_KEYWORDS + _shared._INTENT_CHANGE_VERBS_ZH)))
def test_every_sibling_family_word_blocks_audit_flip(word):
    """三族意图关键词+变更动词族，逐词与审计词共现 ⇒ 绝不翻 AUDIT。"""
    intent = _infer_intent(f"{word} 功能并通过安全审计")
    assert intent != TaskIntent.AUDIT, (
        f"守卫词表成员 {word!r} 与审计词共现时必须挡住 AUDIT 翻转")


@pytest.mark.parametrize("word", sorted(set(_shared._INTENT_CHANGE_VERBS_EN)))
def test_every_english_change_verb_blocks_audit_flip(word):
    """英文变更动词（\\b 词边界正则）逐词与审计词共现 ⇒ 绝不翻 AUDIT。"""
    intent = _infer_intent(f"{word} the module and pass security audit")
    assert intent != TaskIntent.AUDIT, (
        f"英文变更动词 {word!r} 与审计词共现时必须挡住 AUDIT 翻转")


def test_fixture_substring_does_not_count_as_fix():
    """'fixture' 含 fix 子串——不得因此把纯审计请求挡在 AUDIT 之外。"""
    assert _infer_intent("audit the fixture loading for secret scan coverage") == TaskIntent.AUDIT


def test_prefix_with_space_does_not_count_as_fix():
    """hunter LOW-1 实证：'prefix ' 含 'fix ' 子串——英文守卫必须 \\b 词边界，
    否则含 prefix/suffix 的纯审计描述被误挡在 AUDIT 之外。"""
    assert _infer_intent("audit the prefix handling for secret scan coverage") == TaskIntent.AUDIT


# ───────────── 纯审计意图 → 仍 AUDIT，且必须 WARNING 留痕 ─────────────

@pytest.mark.parametrize("desc", [
    "对项目做安全审计",
    "跑一次 sast 扫描",
    "security audit the repository",
])
def test_pure_audit_stays_audit(desc):
    assert _infer_intent(desc) == TaskIntent.AUDIT


def test_audit_flip_emits_machine_readable_warning(caplog):
    """翻转留痕：AUDIT 短路不产 diff 且空 diff 符合预期，静默翻转=劫持链第一环。"""
    with caplog.at_level("WARNING", logger="swarm.brain.nodes.shared"):
        intent = _infer_intent("对项目做安全审计")
    assert intent == TaskIntent.AUDIT
    assert any("intent_audit_inferred" in r.message for r in caplog.records), (
        "AUDIT 启发式翻转必须带机读键 intent_audit_inferred 的 WARNING")


def test_non_audit_inference_no_audit_warning(caplog):
    """反向护栏：非 AUDIT 推断不得刷 intent_audit_inferred 告警（告警要有区分力）。"""
    with caplog.at_level("WARNING", logger="swarm.brain.nodes.shared"):
        _infer_intent("实现用户登录功能并通过安全审计")
    assert not any("intent_audit_inferred" in r.message for r in caplog.records)
