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
