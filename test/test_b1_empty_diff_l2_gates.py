"""#29 B-1 — 非 SIMPLE 路径空 merged_diff 绕过全部确定性 L2 闸的假 DONE 通道。

缺陷：`_verify_l2_impl` 的四道确定性闸【全部】以 `(merged_diff or "").strip()` 为前置：
  :207 project_path 缺失留痕(l2_compile_unverified) / :212 编译+契约 integration_review
  / :497 沙箱·本地功能测试 / :531 无测试命令放行(l2_no_test_executed)
⇒ 非 SIMPLE 任务 + 空 merged_diff ⇒ 一道都不跑，直落 `_verify_l2_via_llm` 让大模型对着
【空 diff】表决；且不置任何 degraded ⇒ verify_l2 薄包装的 coverage 分类器报 'passed'
（而非 'passed:unverified'）⇒ gates.can_auto_accept_delivery 全程不读 merged_diff
⇒ 空产出被 auto_accept 放行，且 blocking_degraded_reasons 为空 ⇒ 被 L6 学成成功模式。
＝端到端完整的假 DONE 通道（实测链路全段已复现）。

SIMPLE 臂本就有这道判据（#74 DR-02-F1 `_diff_has_changes or _diff_has_deletions or
_all_audit` + 逐子任务 `_subtask_produced_expected`），只是【没接到非 SIMPLE 臂】——
血规 10①「接线覆盖 ≠ 机制存在」：机制造对了却只接一个调用点。

治法：判据抽成 `_l2_produced_expected_shape` 单一事实源，非 SIMPLE 臂的空 diff 分支复用：
  · 形态不符预期 → _l2_failure_state fail-closed，绝不交 LLM
  · 形态合法（全 AUDIT 计划：产结构化报告不产 diff）→ 放行，但因【零确定性验证跑过】
    必须携 degraded l2_empty_diff_unverified（接进 passed:unverified 分类器 + L6 阻断集）
"""

from __future__ import annotations

import asyncio

import pytest

from swarm.brain import integration_review as IR
from swarm.brain.nodes import verify as V
from swarm.types import Complexity, TaskIntent, WorkerOutput

NON_SIMPLE = (Complexity.COMPLEX, Complexity.MEDIUM)

NON_EMPTY_DIFF = (
    "diff --git a/x.java b/x.java\n--- a/x.java\n+++ b/x.java\n"
    "@@ -0,0 +1 @@\n+class X {}\n"
)


class _Sub:
    def __init__(self, sid: str, intent=TaskIntent.MODIFY):
        self.id = sid
        self.acceptance_criteria = ["mvn test 通过"]
        self.intent = intent
        self.scope = None


class _Plan:
    def __init__(self, *subs):
        self.subtasks = list(subs)
        self.shared_contract = {}


class _Spy:
    """记录四道确定性闸与 LLM 兜底各被调用几次。"""

    def __init__(self):
        self.integration = 0
        self.sandbox = 0
        self.local = 0
        self.llm = 0

    @property
    def deterministic(self) -> int:
        return self.integration + self.sandbox + self.local


def _run_l2(monkeypatch, *, diff: str, complexity, plan, subtask_results=None,
            llm_verdict: bool = True, integration_ok: bool = True) -> tuple[dict, _Spy]:
    """跑 verify_l2（薄包装，含 coverage 分类），返回 (out, spy)。"""
    from swarm.brain import nodes

    spy = _Spy()

    def _integration(*_a, **_k):
        spy.integration += 1
        return (integration_ok, [],
                {"compile_ran": True, "compile_ok": integration_ok, "stack": "maven"})

    def _sandbox(*_a, **_k):
        spy.sandbox += 1
        return True

    def _local(*_a, **_k):
        spy.local += 1
        return True

    async def _llm(*_a, **_k):
        spy.llm += 1
        return llm_verdict

    monkeypatch.setattr(nodes, "_get_project_path", lambda _p: "/tmp/fake-b1-proj")
    monkeypatch.setattr(IR, "run_integration_review", _integration)
    monkeypatch.setattr(nodes, "_try_l2_sandbox_verify", _sandbox, raising=False)
    monkeypatch.setattr(nodes, "_try_l2_local_verify", _local, raising=False)
    monkeypatch.setattr(nodes, "_verify_l2_via_llm", _llm, raising=False)
    monkeypatch.setattr(V, "effective_complexity", lambda _s: complexity)

    state = {
        "task_id": "t-b1", "project_id": "p1",
        "merged_diff": diff, "plan": plan,
        "task_description": "做点什么",
        "subtask_results": subtask_results or {},
        "complexity": complexity,
    }
    out = asyncio.run(V.verify_l2(state))
    return out, spy


# ══════════════════════════════════════════════════════════
# A) 缺陷本体：空 diff 绝不交 LLM 表决
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("complexity", NON_SIMPLE)
@pytest.mark.parametrize("diff,desc", [
    ("", "空串"),
    ("   \n  \n", "纯空白"),
    ("\t\n", "纯制表/换行"),
])
def test_empty_diff_never_reaches_llm_verdict(monkeypatch, complexity, diff, desc):
    """★空 merged_diff 且形态不符预期 → fail-closed，LLM 一次都不许被调用★

    区分力设计：`llm == 0` 是核心断言 —— 修复前 LLM 被调用 1 次并返回 True，
    只断 `l2_passed is False` 不够（LLM 恰好判 False 时现状也 False，零区分力）。
    """
    plan = _Plan(_Sub("st-1"))
    out, spy = _run_l2(monkeypatch, diff=diff, complexity=complexity, plan=plan,
                       llm_verdict=True)   # ★LLM 若被调用就会说"通过"★
    assert spy.llm == 0, (
        f"{desc}/{complexity.value}: LLM 被调用 {spy.llm} 次 —— 空 diff 交大模型表决")
    assert out.get("l2_passed") is False, f"{desc}/{complexity.value}: 空产出被判通过"
    assert out.get("verification_failure") == "l2"
    assert out.get("verification_coverage") == {"l2": "failed"}


@pytest.mark.parametrize("complexity", NON_SIMPLE)
def test_empty_diff_with_zero_output_subtask_fails(monkeypatch, complexity):
    """子任务 l1_passed=True 但 diff 空（静默零产出）→ 判失败，不靠 LLM。"""
    plan = _Plan(_Sub("st-1"), _Sub("st-2"))
    results = {
        "st-1": WorkerOutput(subtask_id="st-1", diff="", summary="", l1_passed=True),
        "st-2": WorkerOutput(subtask_id="st-2", diff="", summary="", l1_passed=True),
    }
    out, spy = _run_l2(monkeypatch, diff="", complexity=complexity, plan=plan,
                       subtask_results=results, llm_verdict=True)
    assert spy.llm == 0
    assert out.get("l2_passed") is False


@pytest.mark.parametrize("complexity", NON_SIMPLE)
def test_empty_diff_skips_every_deterministic_gate_so_must_not_pass(
        monkeypatch, complexity):
    """★不变量：确定性闸 0 次执行时，绝不允许 l2_passed=True 且 coverage='passed'★

    这条把"哪道闸跑了"与"敢不敢报 passed"绑在一起 —— 是本 finding 的本质：
    零确定性证据的放行必须至少是 passed:unverified。

    ★#29-1R★ 原写法是 `if spy.deterministic == 0 and l2_passed is True: assert ...`
    ——修复后本夹具 l2_passed 恒为 False ⇒ 分支永不进入 ⇒ **断言一次都没跑过**
    （我自己写的 vacuous 绿，reviewer 逮到）。改成无条件断言：先锁前提真的成立
    （确定性闸确实 0 次），再断"passed 与零证据不可共存"这个不变量。
    """
    plan = _Plan(_Sub("st-1"))
    out, spy = _run_l2(monkeypatch, diff="", complexity=complexity, plan=plan)
    cell = (out.get("verification_coverage") or {}).get("l2")
    # 前提自证：空 diff 下确定性闸确实一次没跑（否则本测试测的不是它以为的场景）
    assert spy.deterministic == 0, (
        f"前提不成立：空 diff 却跑了 {spy.deterministic} 次确定性闸，"
        f"本测试的命题（零证据不得报 passed）已不适用于此夹具")
    # 不变量本体（无条件）：零确定性证据 ⇒ 要么不通过，要么至多 passed:unverified
    assert not (out.get("l2_passed") is True and cell == "passed"), (
        f"零确定性验证却报 l2_passed=True + coverage='passed' —— 谎称验过: {cell!r}")


# ══════════════════════════════════════════════════════════
# B) 合法空 diff（全 AUDIT）：放行但必须机读可辨
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("complexity", NON_SIMPLE)
def test_all_audit_plan_empty_diff_passes_with_machine_readable_marker(
        monkeypatch, complexity):
    """全 AUDIT 计划空 diff 是合法形态（产结构化报告不产 diff）→ 放行不误杀，
    但【零确定性验证跑过】必须留机读账，coverage 报 passed:unverified。"""
    plan = _Plan(_Sub("st-1", TaskIntent.AUDIT), _Sub("st-2", TaskIntent.AUDIT))
    results = {
        "st-1": WorkerOutput(subtask_id="st-1", diff="", summary="审计报告", l1_passed=True),
        "st-2": WorkerOutput(subtask_id="st-2", diff="", summary="审计报告", l1_passed=True),
    }
    out, spy = _run_l2(monkeypatch, diff="", complexity=complexity, plan=plan,
                       subtask_results=results)
    assert out.get("l2_passed") is True, "全 AUDIT 空 diff 被误杀"
    assert spy.llm == 0, "合法空 diff 也不该交 LLM 表决（判据是确定性的）"
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert "l2_empty_diff_unverified" in deg, f"缺机读账: {deg}"
    assert (out.get("verification_coverage") or {}).get("l2") == "passed:unverified", (
        "零确定性验证的放行必须是 passed:unverified，不得报 passed")


def test_marker_blocks_l6_success_learning():
    """★新账必须有人消费（血规 10④）★：l2_empty_diff_unverified 不在信息性白名单
    ⇒ blocking_degraded_reasons 认它 ⇒ should_write_success 拦 L6 假学习。

    否则"零验证放行"会被提炼成可复用成功模式写进知识库自毒化。
    """
    from swarm.memory.pattern_extractor import blocking_degraded_reasons
    assert blocking_degraded_reasons(["l2_empty_diff_unverified"]), (
        "l2_empty_diff_unverified 被当成信息性留痕 ⇒ L6 会把零验证交付学成成功模式")


@pytest.mark.parametrize("marker", [
    "l2_no_test_executed",
    "l2_test_downgraded_to_llm",
    "l2_compile_unverified",
    "l2_empty_diff_unverified",   # ← #29 B-1 新增，与上三族同通道
])
def test_unverified_markers_downgrade_coverage_cell(monkeypatch, marker):
    """账值分类器必须认全部"未验证"族前缀 —— 行为级断言（不用 getsource 断字面量，纪律 6）。

    做法：让 _verify_l2_impl 返回 l2_passed=True + 该 marker，看薄包装的 coverage 分类
    是否降档成 passed:unverified。任一族缺席都会让该族的"未验证"放行被报成 passed。
    """
    async def _impl(_state, _handoff):
        return {"l2_passed": True, "degraded_reasons": [marker]}

    monkeypatch.setattr(V, "_verify_l2_impl", _impl)
    out = asyncio.run(V.verify_l2({"task_id": "t", "merged_diff": ""}))
    assert (out.get("verification_coverage") or {}).get("l2") == "passed:unverified", (
        f"{marker} 未接进 coverage 分类器 ⇒ 未验证放行被报成 passed")


def test_coverage_cell_still_passed_when_truly_verified(monkeypatch):
    """反向区分力：无"未验证"族 marker 时 coverage 必须仍是 passed（不是恒 unverified）。

    缺这条时，把分类器改成"无条件 passed:unverified"上面那组也会全绿（零区分力）。
    """
    async def _impl(_state, _handoff):
        return {"l2_passed": True, "degraded_reasons": []}

    monkeypatch.setattr(V, "_verify_l2_impl", _impl)
    out = asyncio.run(V.verify_l2({"task_id": "t", "merged_diff": ""}))
    assert (out.get("verification_coverage") or {}).get("l2") == "passed"


# ══════════════════════════════════════════════════════════
# C) 非空 diff 不受影响（防误杀 / 防改动溢出）
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("complexity", NON_SIMPLE)
def test_non_empty_diff_still_runs_deterministic_gates(monkeypatch, complexity):
    """★对照臂：非空 diff 必须照旧跑确定性闸★ —— 本次改动只针对空 diff 分支，
    若把非空臂也短路了，这条会红（防"修一处伤一片"）。"""
    plan = _Plan(_Sub("st-1"))
    out, spy = _run_l2(monkeypatch, diff=NON_EMPTY_DIFF, complexity=complexity, plan=plan)
    assert spy.integration >= 1, "非空 diff 未跑 integration_review（编译+契约）"
    assert spy.deterministic >= 1
    assert out.get("l2_passed") is True
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert "l2_empty_diff_unverified" not in deg, "非空 diff 被误打空 diff 账"


@pytest.mark.parametrize("complexity", NON_SIMPLE)
def test_pure_deletion_diff_not_treated_as_empty(monkeypatch, complexity):
    """纯删除 diff（只有 - 行 + +++ /dev/null）非空 → 走正常确定性闸，不进空 diff 分支。"""
    delete_diff = (
        "diff --git a/gone.java b/gone.java\n--- a/gone.java\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-class Gone {}\n"
    )
    plan = _Plan(_Sub("st-1"))
    out, spy = _run_l2(monkeypatch, diff=delete_diff, complexity=complexity, plan=plan)
    assert spy.deterministic >= 1, "纯删除 diff 被当空 diff 短路"
    deg = [str(d) for d in (out.get("degraded_reasons") or [])]
    assert "l2_empty_diff_unverified" not in deg


# ══════════════════════════════════════════════════════════
# D) ★纯抽取相等锁★ —— _l2_produced_expected_shape 与 SIMPLE 臂原逻辑逐字等价
# ══════════════════════════════════════════════════════════

def _reference_simple_judgement(merged_diff, plan_obj, subtask_results):
    """SIMPLE 臂【抽取前】的原逻辑副本（逐字复刻，作为相等锁的参照实现）。

    纯结构改动必须加逐元素相等锁（[[swarm-pure-refactor-needs-equality-lock]]：
    拆表手滑跟"行为不变"一起穿过复核的实证）。
    """
    from swarm.brain.nodes.shared import (
        _diff_has_changes, _diff_has_deletions, _subtask_produced_expected,
    )
    merged = (merged_diff or "").strip()
    _all_audit = bool(plan_obj and getattr(plan_obj, "subtasks", None)
                      and all(getattr(t, "intent", None) == TaskIntent.AUDIT
                              for t in plan_obj.subtasks))
    l2_passed = _diff_has_changes(merged) or _diff_has_deletions(merged) or _all_audit
    if subtask_results:
        _by_id = {t.id: t for t in (getattr(plan_obj, "subtasks", None) or [])}

        def _ok(sid, o):
            _lp = ((isinstance(o, WorkerOutput) and o.l1_passed)
                   or (isinstance(o, dict) and o.get("l1_passed", False)))
            if not _lp:
                return False
            _st = _by_id.get(sid)
            return True if _st is None else _subtask_produced_expected(o, _st)

        l2_passed = l2_passed and all(_ok(sid, o) for sid, o in subtask_results.items())
    return l2_passed


_DELETE_DIFF = ("diff --git a/g.java b/g.java\n--- a/g.java\n+++ /dev/null\n"
                "@@ -1 +0,0 @@\n-class G {}\n")

_SHAPE_MATRIX = [
    ("", None, None),
    ("", _Plan(_Sub("s1")), None),
    ("", _Plan(_Sub("s1", TaskIntent.AUDIT)), None),
    ("   \n", _Plan(_Sub("s1", TaskIntent.AUDIT)), None),
    (NON_EMPTY_DIFF, _Plan(_Sub("s1")), None),
    (_DELETE_DIFF, _Plan(_Sub("s1")), None),
    ("", _Plan(_Sub("s1", TaskIntent.AUDIT)),
     {"s1": WorkerOutput(subtask_id="s1", diff="", summary="", l1_passed=True)}),
    ("", _Plan(_Sub("s1", TaskIntent.AUDIT)),
     {"s1": WorkerOutput(subtask_id="s1", diff="", summary="", l1_passed=False)}),
    (NON_EMPTY_DIFF, _Plan(_Sub("s1")),
     {"s1": WorkerOutput(subtask_id="s1", diff=NON_EMPTY_DIFF, summary="", l1_passed=True)}),
    (NON_EMPTY_DIFF, _Plan(_Sub("s1"), _Sub("s2")),
     {"s1": WorkerOutput(subtask_id="s1", diff=NON_EMPTY_DIFF, summary="", l1_passed=True),
      "s2": WorkerOutput(subtask_id="s2", diff="", summary="", l1_passed=True)}),
    (_DELETE_DIFF, _Plan(_Sub("s1"), _Sub("s2")),
     {"s1": WorkerOutput(subtask_id="s1", diff=_DELETE_DIFF, summary="", l1_passed=True),
      "s2": WorkerOutput(subtask_id="s2", diff="", summary="", l1_passed=True)}),
    (NON_EMPTY_DIFF, _Plan(_Sub("s1")), {"s1": {"diff": NON_EMPTY_DIFF, "l1_passed": True}}),
    (NON_EMPTY_DIFF, _Plan(_Sub("s1")), {"s1": {"diff": NON_EMPTY_DIFF}}),  # 缺 l1_passed
    (NON_EMPTY_DIFF, _Plan(), None),
]


@pytest.mark.parametrize("idx", range(len(_SHAPE_MATRIX)))
def test_shape_helper_equals_pre_extraction_logic(idx):
    """★逐格相等锁★：抽出的 helper 与抽取前 SIMPLE 臂逻辑在整张矩阵上结论必须逐字相同。

    覆盖：空/空白/非空/纯删除 diff × 无 plan/普通/AUDIT 计划 × 无结果/WorkerOutput/dict
    /缺 l1_passed。抽取时手滑（漏 and、漏 dict 分支、AUDIT 判据写错）都会在这里红。
    """
    diff, plan, results = _SHAPE_MATRIX[idx]
    got = V._l2_produced_expected_shape(diff, plan, results or {})
    want = _reference_simple_judgement(diff, plan, results or {})
    assert got is want or got == want, (
        f"矩阵第 {idx} 格结论漂移: helper={got} 参照={want} (diff={diff[:30]!r})")


@pytest.mark.parametrize("idx", range(len(_SHAPE_MATRIX)))
def test_simple_arm_behaviour_unchanged_by_extraction(idx, monkeypatch):
    """SIMPLE 臂端到端结论也必须与参照实现一致（helper 相等 ≠ 接线正确）。"""
    diff, plan, results = _SHAPE_MATRIX[idx]
    out, spy = _run_l2(monkeypatch, diff=diff, complexity=Complexity.SIMPLE,
                       plan=plan, subtask_results=results or {})
    want = _reference_simple_judgement(diff, plan, results or {})
    assert out.get("l2_passed") is want, (
        f"SIMPLE 臂第 {idx} 格行为改变: got={out.get('l2_passed')} want={want}")
    assert spy.deterministic == 0 and spy.llm == 0, "SIMPLE 快速路径不该跑闸/LLM"
