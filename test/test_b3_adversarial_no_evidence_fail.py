"""#29 B-3 — 全员判 FAIL 但无凭据 ⇒ 被记成全票通过 + 发 verified token 的假 DONE 通道。

缺陷：`adversarial_verify` 的 verdict 门用 `st.id not in reviewed_ids` 判"未复核"，而
`reviewed_ids` 收的是【出现过的所有 key】（无论 verdict 是 PASS 还是 FAIL）。于是当
全部发声的 reviewer 都判 FAIL、但都没给 `failure_scenario` 时：
  · Pre-Report gate 令 critiques 为空 → 不进 naughty
  · id 却在 reviewed_ids 里 → 不进 unreviewed
  ⇒ 落进 `passed` → 领 `_verified_token` → `adversarial_verify_passed=True`
  ⇒ message 写"经 N 个独立 reviewer 复核均 PASS"（与事实完全相反）
  ⇒ 零 degraded ⇒ 不挡 L6 → 被学成可复用成功模式。

Pre-Report gate 的设计意图（模块 docstring:13）是"FAIL 无凭据 → 降级【不计 FAIL】"
（防小模型无凭据乱 flag），绝不是"计 PASS"。★不计 FAIL ≠ 计 PASS★

治法：显式跟踪【有没有 reviewer 真的判过 PASS】。收到过 FAIL（仅缺凭据）且零 PASS
＝零正面证据，与"漏审"同一认知状态 → 同通道处置（不计 passed / 不发 token / degraded
挡 L6 + 人工可见 / 不硬拦交付，因无 concrete 凭据可回灌，打回也无从修）。
但只要有【任一】reviewer 判 PASS 就仍算通过——那是 Pre-Report gate 刻意的 FP 控制，不动。
"""

from __future__ import annotations

import asyncio

import pytest

from swarm.brain.nodes import adversarial as A
from swarm.types import Complexity, SubTask, TaskIntent, WorkerOutput


def _subtask(sid: str) -> SubTask:
    return SubTask(
        id=sid, description=f"任务 {sid}：做点什么",
        intent=TaskIntent.MODIFY,
        scope={"writable": [f"src/{sid}.java"], "readable": [], "create_files": []},
        acceptance_criteria=["编译通过"],
        depends_on=[],
    )


def _wo(sid: str) -> WorkerOutput:
    return WorkerOutput(
        subtask_id=sid,
        diff=(f"diff --git a/src/{sid}.java b/src/{sid}.java\n"
              f"--- a/src/{sid}.java\n+++ b/src/{sid}.java\n@@ -0,0 +1 @@\n+class A {{}}\n"),
        summary="done", l1_passed=True,
    )


def _run(monkeypatch, verdict_tables, sids=("st-1",), state_extra=None):
    """驱动 adversarial_verify，用给定的 verdict 表替掉真 reviewer。

    patch 点选 _run_one_reviewer（每个 reviewer 一张表），保留节点内全部真实判定逻辑。
    """
    plan_subtasks = [_subtask(s) for s in sids]

    class _Plan:
        subtasks = plan_subtasks
        shared_contract = {}

    tables = list(verdict_tables)
    calls = {"n": 0}

    async def _one(_llm, _messages, _tag):
        i = calls["n"]
        calls["n"] += 1
        return tables[i] if i < len(tables) else None

    monkeypatch.setattr(A, "_run_one_reviewer", _one)
    # 两个"可用"的 reviewer llm（内容无关，_run_one_reviewer 已被替）
    monkeypatch.setattr(A, "_reviewer_llms", lambda: [object(), object()], raising=False)
    from swarm.brain import nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda *a, **k: object(),
                        raising=False)
    monkeypatch.setattr(A, "effective_complexity", lambda _s: Complexity.COMPLEX)

    state = {
        "task_id": "t-b3", "project_id": "p1",
        "plan": _Plan(),
        "task_description": "做点什么",
        "subtask_results": {s: _wo(s) for s in sids},
        "merged_diff": "",
        "complexity": Complexity.COMPLEX,
        **(state_extra or {}),
    }
    return asyncio.run(A.adversarial_verify(state))


def _verified_ids(out) -> list[str]:
    """从 adversarial_verified_ids 的 token 里取出 subtask id。

    token 形态 = `{sid}@{diff_sig}`（_verified_token）—— 按 '@' 右切一次，不用 ':'
    （sid 本身不含 '@'，但用 ':' 切会整串原样返回 ⇒ 断言恒假的假红）。
    """
    return [str(t).rsplit("@", 1)[0] for t in (out.get("adversarial_verified_ids") or [])]


# ══════════════════════════════════════════════════════════
# A) 缺陷本体：全员无凭据 FAIL 绝不算通过
# ══════════════════════════════════════════════════════════

def test_unanimous_fail_without_evidence_is_not_passed(monkeypatch):
    """★全部发声的 reviewer 都判 FAIL（无 failure_scenario）→ 绝不计通过、绝不发 token★

    三维断言：不在 verified_ids（修复前会在）+ 有 all_fail_no_evidence 账 + message 不谎称通过。
    只断 adversarial_verify_passed 不够——它在"不硬拦"语义下仍可为 True（与 unreviewed 同哲学），
    真正的区分点是【有没有领 verified token】。
    """
    out = _run(monkeypatch, [
        {"st-1": ("FAIL", "")},
        {"st-1": ("FAIL", "")},
    ])
    assert "st-1" not in _verified_ids(out), (
        "全票否决的子任务领到了 verified token ⇒ 被记成复核通过")
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert any(d.startswith("adversarial_verify_all_fail_no_evidence") for d in deg), (
        f"缺 all_fail_no_evidence 机读账: {deg}")
    msg = str(out.get("adversarial_verify_message") or "")
    assert "复核均 PASS" not in msg or "未给出具体失败场景" in msg, (
        f"message 谎称全部通过: {msg}")


@pytest.mark.parametrize("n_reviewers", [1, 2, 3])
def test_all_speaking_reviewers_fail_no_evidence(monkeypatch, n_reviewers):
    """reviewer 数量无关：只要【发声的都判 FAIL 且都无凭据】就不算通过。"""
    out = _run(monkeypatch, [{"st-1": ("FAIL", "")} for _ in range(n_reviewers)])
    assert "st-1" not in _verified_ids(out)


def test_mixed_batch_only_no_evidence_one_is_held(monkeypatch):
    """混合批：st-1 全员无凭据 FAIL / st-2 有凭据 FAIL / st-3 全 PASS
    → st-1 不通过不打回、st-2 打回、st-3 正常通过。三条互不串味。"""
    out = _run(monkeypatch, [
        {"st-1": ("FAIL", ""), "st-2": ("FAIL", "越界"), "st-3": ("PASS", "")},
        {"st-1": ("FAIL", ""), "st-2": ("FAIL", "越界"), "st-3": ("PASS", "")},
    ], sids=("st-1", "st-2", "st-3"))
    vids = _verified_ids(out)
    assert "st-1" not in vids, "全员无凭据 FAIL 者领到 token"
    assert "st-2" not in vids, "有凭据 FAIL 者领到 token"
    assert "st-3" in vids, "真通过者被误伤"
    # st-2 走 naughty 打回；st-1 不打回（无 concrete 凭据可回灌）
    assert "st-2" in (out.get("failed_subtask_ids") or [])
    assert "st-1" not in (out.get("failed_subtask_ids") or []), (
        "无凭据 FAIL 被打回 —— 无从修，会空转到放弃")


# ══════════════════════════════════════════════════════════
# B) 反向：Pre-Report gate 的 FP 控制不得被破坏
# ══════════════════════════════════════════════════════════

def test_one_pass_still_counts_as_passed(monkeypatch):
    """★有任一 reviewer 判 PASS → 仍算通过（Pre-Report gate 刻意的 FP 控制，不动）★

    "防小模型无凭据乱 flag"是既有设计（docstring:13）。若本次修复把这条也拧成不通过，
    单个小模型的无凭据 FAIL 就能掐死交付 —— 这条锁住那个边界。
    """
    out = _run(monkeypatch, [
        {"st-1": ("FAIL", "")},     # 无凭据 FAIL
        {"st-1": ("PASS", "")},     # 有人明确判过
    ])
    assert "st-1" in _verified_ids(out), "有 PASS 却不算通过 ⇒ 无凭据 FAIL 可单方面掐死交付"
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert not any(d.startswith("adversarial_verify_all_fail_no_evidence") for d in deg), (
        "有 PASS 时不该记 all_fail_no_evidence")


def test_unanimous_pass_unaffected(monkeypatch):
    """全 PASS → 正常通过、无新账（防改动溢出）。"""
    out = _run(monkeypatch, [{"st-1": ("PASS", "")}, {"st-1": ("PASS", "")}])
    assert "st-1" in _verified_ids(out)
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert not any("all_fail_no_evidence" in d for d in deg)
    assert out.get("adversarial_verify_passed") is True


def test_fail_with_evidence_still_flags_back(monkeypatch):
    """有凭据 FAIL → 照旧 NAUGHTY 打回 + l1_passed 置 False（既有主路径不受影响）。"""
    out = _run(monkeypatch, [
        {"st-1": ("FAIL", "输入 null 时 NPE")},
        {"st-1": ("PASS", "")},
    ])
    assert out.get("adversarial_verify_passed") is False
    assert "st-1" in (out.get("failed_subtask_ids") or [])
    res = out.get("subtask_results") or {}
    assert res["st-1"].l1_passed is False
    assert "adversarial_critique" in (res["st-1"].l1_details or {})


# ══════════════════════════════════════════════════════════
# C) 两种"零正面证据"必须机读可辨（血规 10③ 分档）
# ══════════════════════════════════════════════════════════

def test_missing_verdict_and_no_evidence_fail_are_distinguishable(monkeypatch):
    """★漏审 与 全员无凭据 FAIL 必须落不同 degraded 键★

    前者=reviewer 根本没发声（无任何信号）；后者=reviewer 明确认为有问题只是没给场景
    （有负面信号但不可行动）。混成一条会让"全票否决"在账面上长得跟"没人来审"一样，
    复盘时无从区分（血规 10③：共享通道可以，后果语义不同就必须分档）。
    """
    # st-1 漏审（两张表都不含它）；st-2 全员无凭据 FAIL
    out = _run(monkeypatch, [
        {"st-2": ("FAIL", "")},
        {"st-2": ("FAIL", "")},
    ], sids=("st-1", "st-2"))
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    cov = [d for d in deg if d.startswith("adversarial_verify_incomplete_coverage")]
    nev = [d for d in deg if d.startswith("adversarial_verify_all_fail_no_evidence")]
    assert cov, f"漏审账缺失: {deg}"
    assert nev, f"无凭据 FAIL 账缺失: {deg}"
    assert "st-1" in cov[0], f"漏审账未含 st-1: {cov}"
    assert "st-2" in nev[0], f"无凭据 FAIL 账未含 st-2: {nev}"
    assert _verified_ids(out) == [], "两者都不该领 token"


def test_new_marker_blocks_l6_success_learning():
    """★新账必须有人消费（血规 10④）★：all_fail_no_evidence 不在信息性白名单
    ⇒ blocking_degraded_reasons 认它 ⇒ should_write_success 拦 L6 假学习。"""
    from swarm.memory.pattern_extractor import blocking_degraded_reasons
    assert blocking_degraded_reasons(["adversarial_verify_all_fail_no_evidence:st-1"]), (
        "全票否决的交付会被 L6 学成成功模式")


def test_new_marker_does_not_hard_block_auto_accept():
    """★边界诚实：本账【不硬拦】auto_accept（与 incomplete_coverage 同哲学）★

    理由（模块 docstring:15-21）：无 concrete failure_scenario 就没有可回灌的凭据，
    打回也无从修；硬拦会在 reviewer 普遍不给场景时 strand 全部交付=新黑洞。
    只有 unconverged（有负面证据未解决）才硬拦。这条把该边界写死，防后来人误以为
    "记了账就等于拦住了"。
    """
    from swarm.brain.gates import can_auto_accept_delivery
    state = {
        "l2_passed": True, "l3_passed": True,
        "degraded_reasons": ["adversarial_verify_all_fail_no_evidence:st-1"],
        "subtask_results": {}, "plan": None,
    }
    allow, _reason = can_auto_accept_delivery(state)
    assert allow is True, (
        "本账被拧成硬拦 —— 与 incomplete_coverage 同哲学的边界被改，"
        "reviewer 普遍不给场景时会 strand 全部交付")


# ══════════════════════════════════════════════════════════
# D) ★#29-1R★ R53-4 熔断态不得丢账
#
# 对抗复核（reviewer 透镜）逮到的既有 bug，我的新账也掉进去：`_skip` 原签名收
# `degraded: str | None` 且 `out["degraded_reasons"] = [degraded]` 是**整体赋值**
# （非 append）。于是熔断分支只传自己那一条 advisory，本轮已聚合的
# incomplete_coverage / all_fail_no_evidence **一起蒸发**——而熔断态
# （已升人工、复核降 advisory）恰恰是这些账最该留的时候。
# ══════════════════════════════════════════════════════════

_ESCALATED = {"degraded_reasons": ["adversarial_verify_unconverged:round_cap_2"]}


def test_escalated_branch_keeps_all_degraded_accounts(monkeypatch):
    """★熔断态下 advisory 与本轮聚合账必须共存（原实现只剩 advisory）★

    构造：任务已因未收敛升过人工（R53-4 粘滞熔断态）+ 本轮 st-1 全员无凭据 FAIL
    + st-2 有凭据 FAIL（令 naughty 非空，才进得了熔断分支）。
    """
    out = _run(monkeypatch, [
        {"st-1": ("FAIL", ""), "st-2": ("FAIL", "越界写了别人的文件")},
        {"st-1": ("FAIL", ""), "st-2": ("FAIL", "越界写了别人的文件")},
    ], sids=("st-1", "st-2"), state_extra=_ESCALATED)
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert any(d.startswith("adversarial_verify_advisory_after_escalation")
               for d in deg), f"缺 advisory 账（熔断分支没走到？）: {deg}"
    assert any(d.startswith("adversarial_verify_all_fail_no_evidence") for d in deg), (
        f"★熔断态把 all_fail_no_evidence 账冲掉了（整体赋值复发）: {deg}")
    assert any(d.startswith("adversarial_verify_incomplete_coverage") for d in deg), (
        f"★熔断态把 incomplete_coverage 账冲掉了（既有 bug 复发）: {deg}")


def test_escalated_branch_does_not_flag_back(monkeypatch):
    """★区分力：熔断态仍不打回（R53-4 语义不得被本次改动破坏）★

    缺了这条，上面那条会被"干脆不走熔断分支"这种改法满足。
    """
    out = _run(monkeypatch, [
        {"st-1": ("FAIL", "越界")},
        {"st-1": ("FAIL", "越界")},
    ], sids=("st-1",), state_extra=_ESCALATED)
    assert out.get("adversarial_verify_passed") is None, (
        "熔断态被打回（R53-4：熔断=交人工，不是再来一轮）")
    assert not out.get("failed_subtask_ids"), "熔断态把子任务记进 failed（不该打回）"


def test_non_escalated_control_still_has_both_accounts(monkeypatch):
    """对照：非熔断态同样输入，两条账本来就在（证明上面测的是熔断分支特有的丢账）。"""
    out = _run(monkeypatch, [
        {"st-1": ("FAIL", ""), "st-2": ("FAIL", "越界写了别人的文件")},
        {"st-1": ("FAIL", ""), "st-2": ("FAIL", "越界写了别人的文件")},
    ], sids=("st-1", "st-2"))
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert any(d.startswith("adversarial_verify_all_fail_no_evidence") for d in deg), deg
    assert any(d.startswith("adversarial_verify_incomplete_coverage") for d in deg), deg


def test_skip_helper_accepts_str_and_list():
    """`_skip` 的 degraded 形参双形态（str 兼容既有 6 个调用点 / list 供熔断分支）。

    直接测 helper：既有调用点全传 str，若把形参改成只收 list，那 6 处会静默
    把字符串拆成逐字符列表——这条锁住兼容性。
    """
    out_s = A._skip(0, [], "m", degraded="a")
    assert out_s["degraded_reasons"] == ["a"], out_s
    out_l = A._skip(0, [], "m", degraded=["a", "b"])
    assert out_l["degraded_reasons"] == ["a", "b"], out_l
    out_n = A._skip(0, [], "m")
    assert "degraded_reasons" not in out_n, "无降级时不该发空账"
    # 去重保序 + 空串剔除
    out_d = A._skip(0, [], "m", degraded=["a", "a", "", "b"])
    assert out_d["degraded_reasons"] == ["a", "b"], out_d


# ══════════════════════════════════════════════════════════
# E) ★#29-1R F5★ 对抗复核结论必须进人工闸视野
#
# 原状：adversarial_verify_message / _details 全仓**零读者**（只写不读），而
# brain/state.py:249-250 自称"deliver 展示/失败回灌数据源"＝过期承诺。
# 后果具体：B-3 修的那句「N 个被判 FAIL 但未给出具体失败场景」写进了 message，
# 人工在交付面上**根本看不到** ⇒ 修了个看不见的字符串（血规 10④：没有消费者＝没造）。
# ══════════════════════════════════════════════════════════

def test_deliver_payload_carries_adversarial_conclusion(monkeypatch):
    """★端到端：节点产出的 message/details 必须出现在人工闸 payload 里★

    真跑节点取产出 → 喂 _deliver_review_payload（人工闸的真实组装函数），
    不手工拼 state 形状（避免"测试自造的形状与生产写者不一致"的假接线）。
    """
    from swarm.brain.nodes import _deliver_review_payload

    # 全 NICE 臂（无 naughty）——B-3 的诚实措辞就写在这条臂的 message 里
    out = _run(monkeypatch, [
        {"st-1": ("FAIL", "")},
        {"st-1": ("FAIL", "")},
    ], sids=("st-1",))
    adv = _deliver_review_payload(out).get("adversarial")
    assert isinstance(adv, dict), "payload 无 adversarial 段（F5 未接线）"
    assert adv.get("message"), "对抗复核结论 message 未进人工闸视野"
    assert "未给出具体失败场景" in adv["message"], (
        f"B-3 的诚实措辞没进 payload ⇒ 修了个看不见的字符串: {adv['message']}")


def test_deliver_payload_carries_flagback_critiques(monkeypatch):
    """★打回臂：逐子任务评语原文必须进人工闸★

    与上条分开测是因为两条臂的 message 是**不同字符串**（全 NICE 臂含 B-3 措辞，
    打回臂是"打回 N 个"）—— 混在一条里会让断言只能取交集＝弱化命题。
    """
    from swarm.brain.nodes import _deliver_review_payload

    out = _run(monkeypatch, [
        {"st-1": ("FAIL", ""), "st-2": ("FAIL", "越界写了别人的文件")},
        {"st-1": ("FAIL", ""), "st-2": ("FAIL", "越界写了别人的文件")},
    ], sids=("st-1", "st-2"))
    adv = _deliver_review_payload(out)["adversarial"]
    crit = adv.get("critiques") or {}
    assert "st-2" in crit, f"有凭据的 FAIL 评语未进人工闸: {crit}"
    assert any("越界" in c for c in crit["st-2"]), f"评语原文丢失: {crit}"
    # 无凭据的那个不在 critiques（它没评语）——但它的账在 degraded 里，两条通道分工明确
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert any("all_fail_no_evidence" in d for d in deg), (
        f"无凭据 FAIL 在打回臂上既无评语又无账 ⇒ 人工完全看不到: {deg}")


def test_deliver_payload_adversarial_tolerates_old_checkpoint():
    """旧 checkpoint 无这些键 → 缺省不炸（deliver 是 interrupt 锚点，组装失败=人工闸打不开）。"""
    from swarm.brain.nodes import _deliver_review_payload
    for state in ({}, {"adversarial_verify_details": None},
                  {"adversarial_verify_details": "not-a-dict"},
                  {"adversarial_verify_details": {"st-1": "not-a-dict"}}):
        adv = _deliver_review_payload(state)["adversarial"]
        assert adv["passed"] is None or adv["passed"] in (True, False)
        assert isinstance(adv["critiques"], dict)


def test_deliver_payload_adversarial_pass_case_is_visible():
    """反向区分力：通过态也要如实出现（否则人工只在失败时看得见＝半接线）。"""
    from swarm.brain.nodes import _deliver_review_payload
    adv = _deliver_review_payload({
        "adversarial_verify_passed": True,
        "adversarial_verify_round": 0,
        "adversarial_verify_message": "对抗验证通过：2 个子任务经 2 个独立 reviewer 复核均 PASS",
    })["adversarial"]
    assert adv["passed"] is True
    assert "复核均 PASS" in adv["message"]
