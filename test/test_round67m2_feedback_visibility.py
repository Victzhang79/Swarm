"""R67M2-T1（24号文，round67m2 FAILED@PLAN CRITICAL 主死因治本）：A1 阻断级反馈
全量可见 + A2 H-5 熔断前提随动。

round67m2 实证的三层合谋致命环（反馈层×熔断交互）：
  - 轮2 G1 打回池 17 条（③b 同名异包×6 + ③f shadow×11，约 1 万字符）超旧 8000 帽
    被分 2 页；retry=1 只展示第 2/2 页（2 条非致命 sysrole/userrealm），3 条致命
    ③b 全在未展示页 → 轮3 全部 21 个 plan_batch prompt 对主死因失明；
  - 轮3 同族重犯（签名去 st-id 逐字相同）→ H-5 以"带反馈重产未收敛"熔断顶格——
    但反馈从未送达，"未收敛"前提被证伪：熔断打在轮转即将送回致命页的前一轮。

治本：
  A1 全量可见帽 8000→32000（现实打回池全显，分页轮转仅剩病态池安全阀语义）；
  A2 熔断账增 over_budget/over_budget_1ago（与反馈格式化同源同帽）——历轮反馈
     曾超帽分页 ⇒ 同签名/窗口触发暂缓，给轮转送达留轮次（MAX_PLAN_RETRY 硬顶格兜底）。
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.nodes as _n  # noqa: E402
from swarm.brain.graph import MAX_PLAN_RETRY  # noqa: E402
from swarm.brain.nodes import (  # noqa: E402
    _feedback_over_budget,
    _format_validation_feedback,
    _gate_fuse_and_account,
)


def _wheel2_pool():
    """round67m2 轮2 打回池形态：③b 长文×6 + ③f 长文×11 ≈ 17 条 × ~600 字符 ≈ 10K。
    超旧帽 8000（旧码必分 2 页且 retry=1 只见 2 条），低于新帽 32000（必须全显）。"""
    dup = [f"③b 同名异包重复 create：类 DupClass{i} 被多个子任务创建"
           f"（com.example.mod{a}.DupClass{i} vs com.example.mod{b}.DupClass{i}）——"
           f"启动期 bean/typeAlias 冲突必崩；" + "详述" * 260
           for i, (a, b) in enumerate([("alpha", "beta"), ("gamma", "delta"),
                                       ("eps", "zeta"), ("eta", "theta"),
                                       ("iota", "kappa"), ("lambda", "mu")])]
    shadow = [f"③f create_vs_base_modify_shadow：符号 ShadowClass{j} 的 create 落点"
              f"与 base 既有实体同 simple name（ruoyi-module{j}/src/main/java/...）——"
              f"启动期 simple name 撞 base bean 必崩；" + "详述" * 260
              for j in range(11)]
    return dup + shadow


# ─── A1：阻断级打回池全量可见 ───


def test_wheel2_shape_pool_fully_visible_at_any_rotate():
    """轮2 池形态（17 条 ≈10K）：旧码 retry=1 只见第 2/2 页 2 条 → 新码任何 rotate 全显。"""
    pool = _wheel2_pool()
    joined_len = len("\n".join(f"- {s}" for s in pool))
    assert 8000 < joined_len < 32000, "夹具必须卡在旧帽之上新帽之下（否则钉不住回归）"
    for rotate in (0, 1, 2):
        fb = _format_validation_feedback(pool, rotate=rotate)
        assert "轮转" not in fb, "现实打回池绝不许分页藏阻断级 issue（round67m2 失明面）"
        for s in pool:
            assert s[:40] in fb, f"rotate={rotate} 时每条阻断级 issue 都必须可见"


def test_over_budget_pool_still_rotates_with_blocking_header():
    """病态池安全阀：>32000 仍分页轮转，页头明示未展示页同为阻断级、回卷可达。"""
    pool = [f"需求条目 req-{i:06d} 未被覆盖：" + "占位" * 60 for i in range(900)]
    fb0 = _format_validation_feedback(pool, rotate=0)
    fb1 = _format_validation_feedback(pool, rotate=1)
    assert "轮转" in fb0 and "阻断级" in fb0
    assert fb0 != fb1, "安全阀分页必须逐轮轮转（A9 round34 治本的保留语义）"


def test_over_budget_helper_agrees_with_formatter():
    """A2 前提判定与反馈格式化同源同帽：边界两侧 over_budget 判定与实际分页一致。"""
    small = _big_pool("小", 100)   # ≈10.2K < 32000
    big = _big_pool("大", 900)     # ≈92K  > 32000
    assert _feedback_over_budget(small) is False
    assert "轮转" not in _format_validation_feedback(small)
    assert _feedback_over_budget(big) is True
    assert "轮转" in _format_validation_feedback(big)
    # 去重后低于帽也绝不判超帽（重复长文不得虚报分页）
    dedup = ["y" * 10000, "y" * 10000]
    assert _feedback_over_budget(dedup) is False


# ─── A2：熔断前提随动（历轮超帽分页 → 触发暂缓） ───


def _same_sig_pool():
    return ["G1 模块 coherence 违例：alarm-api 一对多物理根"]


def _big_pool(tag, n):
    """不同条目病态池（~100 字符/条，去重免疫——重复串会被 _dedup_issue_lines 并掉）。"""
    return [f"{tag}违例{i:04d}-" + "x" * 90 for i in range(n)]


def test_fuse_sig_deferred_when_prev_feedback_paginated(caplog):
    """轮2 池超帽分页（over_budget=True）→ 轮3 同签名【绝不熔断】：'未收敛'前提证伪，
    给轮转送达留轮次；且暂缓面必须 WARNING 可观测（降级绝不静默）。"""
    r_retry, r_acct = _gate_fuse_and_account(
        {}, "G1", _big_pool("甲", 900), 0)   # 超帽池 → over_budget=True
    assert r_acct["over_budget"] is True
    with caplog.at_level(logging.WARNING):
        retry_out, acct = _gate_fuse_and_account(
            {"plan_validation_prev_structural": r_acct}, "G1", _big_pool("甲", 900), 1)
    assert retry_out == 1, "历轮反馈未全量送达时同签名绝不许熔断（round67m2 误顶格面）"
    assert acct["over_budget_1ago"] is True, "账必须把超帽史传给下轮（窗口触发取证用）"
    assert any("熔断暂缓" in r.message for r in caplog.records), \
        "熔断暂缓必须 WARNING 留痕（降级可观测）"


def test_fuse_sig_fires_when_feedback_fully_delivered(caplog):
    """历轮反馈均全量送达（未超帽）+ 同签名连续两轮 → 熔断照火（前提成立，绝不被 A2 误缓）。"""
    r0_retry, r0 = _gate_fuse_and_account({}, "G1", _same_sig_pool(), 0)
    assert r0["over_budget"] is False
    with caplog.at_level(logging.WARNING):
        retry_out, _ = _gate_fuse_and_account(
            {"plan_validation_prev_structural": r0}, "G1", _same_sig_pool(), 1)
    assert retry_out >= MAX_PLAN_RETRY, "反馈全量送达仍同签名=真不收敛，熔断必须照火"
    assert any("已全量送达" in r.message for r in caplog.records), \
        "熔断日志必须明示前提（反馈已全量送达）——复盘归因口径"


def test_fuse_sig_fires_after_delivery_catches_up():
    """轮转送达后（上上轮超帽、上轮已全显）同签名 → 触发一照火：LLM 已被告知仍不改。"""
    _, big = _gate_fuse_and_account({}, "G1", _big_pool("甲", 900), 0)  # 超帽
    _, small = _gate_fuse_and_account(
        {"plan_validation_prev_structural": big}, "G1", _same_sig_pool(), 1)  # 全显
    retry_out, _ = _gate_fuse_and_account(
        {"plan_validation_prev_structural": small}, "G1", _same_sig_pool(), 2)
    assert retry_out >= MAX_PLAN_RETRY, \
        "上轮反馈已全量送达（over_budget=False）后同签名=前提恢复，触发一必须照火"


def test_fuse_window_deferred_on_partial_history():
    """窗口触发跨两轮取证：历轮反馈连续超帽（计数不降是反馈失明的必然）→ 窗口熔断暂缓。"""
    _, b0 = _gate_fuse_and_account({}, "G1", _big_pool("甲", 900), 0)   # 超帽 count=900
    _, b1 = _gate_fuse_and_account(
        {"plan_validation_prev_structural": b0}, "G1", _big_pool("乙", 950), 1)  # 仍超帽
    retry_out2, _ = _gate_fuse_and_account(
        {"plan_validation_prev_structural": b1}, "G1", _big_pool("丙", 960), 2)
    assert retry_out2 == 2, "历轮反馈连续超帽未送达时窗口计数不降绝不许熔断（前提证伪）"


def test_nonconsecutive_partial_history_no_deferral():
    """非连续（中间新周期）→ over_budget_1ago 不继承（None）：陈旧超帽史绝不误暂缓。"""
    _, a0 = _gate_fuse_and_account({}, "G1", _big_pool("甲", 900), 0)
    _, a1 = _gate_fuse_and_account(
        {"plan_validation_prev_structural": a0}, "G1", _same_sig_pool(), 3)  # 跳轮=非连续
    assert a1["over_budget_1ago"] is None, "非连续绝不继承陈旧超帽史（与 gate_1ago 同律）"


def test_legacy_account_without_over_budget_unchanged():
    """在飞 checkpoint 旧账无 over_budget 键 → 按未超帽论，熔断行为与升级前逐字一致。"""
    legacy = {"gate": "G1", "sig": None, "retry": 0, "count": 1}
    from swarm.brain.plan_validator import normalize_structural_signature as _norm
    legacy["sig"] = _norm(_same_sig_pool())
    retry_out, acct = _gate_fuse_and_account(
        {"plan_validation_prev_structural": legacy}, "G1", _same_sig_pool(), 1)
    assert retry_out >= MAX_PLAN_RETRY, "旧账兼容面：升级瞬间熔断防护绝不缺位"
    assert acct["over_budget"] is False


# ─── hunter R1 整改：H-5 熔断 count/sig 与展示面同去重口径 ───


def test_fuse_count_sig_use_deduped_pool():
    """hunter H-5：900 条【重复】issue——LLM 实际只被告知 1 条（_dedup_issue_lines），
    熔断 count/sig 必须同口径（未去重的 count=900 会把窗口/签名触发虚撑到熔断）。"""
    dup = ["同一条违例文本"] * 900
    _, acct = _gate_fuse_and_account({}, "G1", dup, 0)
    assert acct["count"] == 1, "count 必须是去重后口径（LLM 实际所见）"
    assert acct["over_budget"] is False, "去重后 1 条绝不许判超帽"
    # 去重口径下同签名连续两轮=真不收敛（LLM 已被完整告知那 1 条）→ 熔断照火
    retry_out, _ = _gate_fuse_and_account(
        {"plan_validation_prev_structural": acct}, "G1", dup, 1)
    assert retry_out >= MAX_PLAN_RETRY


# ─── hunter R1 整改：H-2 prompt 组装分段预算隔离 ───


def test_no_regress_block_bounded_with_count_marker():
    """hunter H-2：反回归段定界——病态历轮账（300 条）超预算按 bullet 截断+计数明示，
    绝不静默丢、绝不出界把 prev_plan/sliding 原文挤出总帽（H-1 失明面复发）。"""
    from swarm.brain.nodes import _NO_REGRESS_BLOCK_BUDGET, _no_regress_feedback_block
    hist = [f"历轮缺陷{i:04d}-" + "长" * 90 for i in range(300)]
    block = _no_regress_feedback_block({
        "plan_validation_issue_history": hist, "plan_validation_issues": []})
    assert len(block) <= _NO_REGRESS_BLOCK_BUDGET + 200, "反回归段必须定界（页头余量内）"
    assert "及其余" in block and "绝不许回归" in block, "截断必须计数明示（绝不静默）"
    assert "历轮缺陷0000" in block, "头部 bullet 存活"
    # 小账零变化（无截断噪声）
    small = _no_regress_feedback_block({
        "plan_validation_issue_history": ["缺陷甲"], "plan_validation_issues": []})
    assert "及其余" not in small and "缺陷甲" in small


def test_sliding_ctx_budget_isolation_invariant():
    """hunter H-2：分段预算隔离不变量——最坏形态（主反馈满页 + 反回归段满帽 + 修补块
    既有分段帽 ~9K）组装后仍整包装进 _SLIDING_CTX_BUDGET，截断只落 sliding 原文尾部。"""
    from swarm.brain.nodes import (
        _FEEDBACK_PAGE_BUDGET,
        _NO_REGRESS_BLOCK_BUDGET,
        _SLIDING_CTX_BUDGET,
    )
    feedback_max = _format_validation_feedback(_big_pool("满", 900), rotate=0)
    assert len(feedback_max) <= _FEEDBACK_PAGE_BUDGET + 300, "主反馈分页页自带预算"
    from swarm.brain.nodes import _no_regress_feedback_block
    no_regress_max = _no_regress_feedback_block({
        "plan_validation_issue_history": [f"历轮缺陷{i:04d}-" + "长" * 90
                                          for i in range(300)],
        "plan_validation_issues": []})
    guidance = len(feedback_max) + len(no_regress_max) + 9200 + 500  # 修补块帽+头部余量
    assert guidance <= _SLIDING_CTX_BUDGET, (
        f"结构块最坏合计 {guidance} 必须装进总帽 {_SLIDING_CTX_BUDGET}"
        "（否则 H-1 整块截没面复发）")
