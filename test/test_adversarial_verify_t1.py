"""T1（round37b，ECC §B santa-method 移植）：对抗验证 stage — 复核子任务"声称成功"。

定案依据 docs/ECC_TRANSPLANT_REGISTER §B + memory/swarm-ecc-mechanism-transplant §5：
swarm 现只有 HANDLE_FAILURE（管失败），对自报 done（L1 通过）的子任务【无独立复核】——
round36"自造 TwoFactorSetupVO 却自认成功"就该被这一 stage 拦下。移植 santa 内核：
  · 两个【独立】reviewer（GLM×Kimi 交叉=真模型多样性），无共享上下文；
  · 任一 FAIL → 该子任务 NAUGHTY 打回（"单人抓到即真，另一人漏=要消灭的盲区"）；
  · MAX_ITER≤N 不收敛→升人工（degraded 可观测，绝不静默放行）；
  · 只修 flag 项，flag-back 复用既有 failed_subtask_ids→handle_failure 重试预算（双界收敛）。
移植 code-reviewer 的 Pre-Report Gate：FAIL 必带 concrete failure_scenario，否则降级不计（防小模型乱 flag）。

栈无关（北极星）：抽象 subtask/diff/契约，rubric 无语言/框架词汇。跑测试用 .venv/bin/python -m pytest。
"""

from __future__ import annotations

import asyncio
import json

import swarm.brain.nodes as nodes
from swarm.types import Complexity, FileScope, SubTask, TaskIntent, TaskPlan, WorkerOutput

# ───────────────────────── stub reviewer ─────────────────────────

class _FakeReviewer:
    """记账式假 reviewer：按 subtask_id→(verdict, failure_scenario) 回结构化 JSON。

    verdict='FAIL' 且 failure_scenario 非空 → 真 flag；failure_scenario 空 → Pre-Report
    gate 应降级不计（模拟小模型无凭据乱 flag）。"""

    def __init__(self, verdicts: dict[str, tuple[str, str]], tag: str = "A"):
        self.verdicts = verdicts
        self.tag = tag
        self.calls = 0
        self.last_prompt = None

    async def ainvoke(self, messages):
        self.calls += 1
        self.last_prompt = messages
        reviews = [
            {"subtask_id": sid, "verdict": v[0],
             "issue": ("捏造引用" if v[0] == "FAIL" else ""),
             "failure_scenario": v[1]}
            for sid, v in self.verdicts.items()
        ]
        return type("R", (), {"content": json.dumps({"reviews": reviews})})()


class _DeadReviewer:
    """基建故障：ainvoke 立即抛（模拟 provider 挂/超时）→ 该 reviewer 不可用。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise asyncio.TimeoutError()


# ───────────────────────── 夹具 ─────────────────────────

def _plan(*ids):
    subs = [
        SubTask(id=i, description=f"实现 {i}", scope=FileScope(writable=[f"{i}.x"]),
                covers=[f"req-{i}"])
        for i in ids
    ]
    return TaskPlan(subtasks=list(subs), parallel_groups=[list(ids)])


def _wo(sid, diff="+ new code line\n", l1=True):
    return WorkerOutput(subtask_id=sid, diff=diff, summary="变更", l1_passed=l1)


def _state(ids=("st-1", "st-2"), complexity=Complexity.COMPLEX, **extra):
    st = {
        "complexity": complexity,
        "plan": _plan(*ids),
        "subtask_results": {i: _wo(i) for i in ids},
        "dispatch_remaining": [],
        "failed_subtask_ids": [],
    }
    st.update(extra)
    return st


def _wire(monkeypatch, primary, fallback):
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: primary)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: fallback)


def _run(state):
    from swarm.brain.nodes.adversarial import adversarial_verify
    return asyncio.run(adversarial_verify(state))


# ───────────────────────── 核心 verdict 门 ─────────────────────────

def test_dual_review_both_pass_routes_merge(monkeypatch):
    """两个独立 reviewer 都 PASS → 放行（passed=True），候选进 verified_ids，两模型各调一次。"""
    from swarm.brain.nodes.adversarial import _verified_token
    a = _FakeReviewer({"st-1": ("PASS", ""), "st-2": ("PASS", "")}, "A")
    b = _FakeReviewer({"st-1": ("PASS", ""), "st-2": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    st = _state()
    out = _run(st)
    assert out["adversarial_verify_passed"] is True
    assert not out.get("failed_subtask_ids")
    toks = {_verified_token(i, st["subtask_results"][i]) for i in ("st-1", "st-2")}
    assert set(out["adversarial_verified_ids"]) == toks  # 内容绑定 token（非裸 id）
    assert a.calls == 1 and b.calls == 1, "GLM×Kimi 交叉双复核，各一次"


def test_either_reviewer_fail_flags_subtask(monkeypatch):
    """任一 reviewer FAIL（带 failure_scenario）→ 该子任务 NAUGHTY 打回，兄弟不受累。"""
    from swarm.brain.nodes.adversarial import _verified_token
    a = _FakeReviewer({"st-1": ("PASS", ""),
                       "st-2": ("FAIL", "链接到无生产者的捏造类型 → 运行期 NoSuchMethodError")}, "A")
    b = _FakeReviewer({"st-1": ("PASS", ""), "st-2": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    st = _state()
    out = _run(st)
    assert out["adversarial_verify_passed"] is False
    assert "st-2" in out["failed_subtask_ids"]
    assert "st-1" not in out.get("failed_subtask_ids", [])
    tok1 = _verified_token("st-1", st["subtask_results"]["st-1"])
    assert tok1 in out["adversarial_verified_ids"], "只过复核的兄弟入 verified，下轮不重审"
    # NAUGHTY 子任务 l1_passed 置 False：依赖者须等待 + handle_failure 据此重做（复用既有预算）
    assert out["subtask_results"]["st-2"].l1_passed is False
    assert out["subtask_results"]["st-1"].l1_passed is True
    # 复核评语进 l1_details，供 handle_failure 注入 retry_guidance（换模型重试不重蹈）
    assert out["subtask_results"]["st-2"].l1_details.get("adversarial_critique")


def test_fail_without_failure_scenario_not_flagged(monkeypatch):
    """Pre-Report gate：FAIL 无 concrete failure_scenario → 降级不计（防小模型乱 flag）。"""
    a = _FakeReviewer({"st-1": ("FAIL", "")}, "A")   # 空 failure_scenario = 噪声
    b = _FakeReviewer({"st-1": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    out = _run(_state(ids=("st-1",)))
    assert out["adversarial_verify_passed"] is True
    assert "st-1" not in out.get("failed_subtask_ids", [])


def test_hallucinated_producer_type_caught(monkeypatch):
    """round36 复现：子任务引用无人生产的类型，一个 reviewer 抓到 → 判 FAIL 打回（非通过）。"""
    a = _FakeReviewer({"st-1": ("PASS", "")}, "A")   # A 漏了（盲区）
    b = _FakeReviewer({"st-1": ("FAIL", "引用 SysUser2FAServiceImpl 未在任何子任务/契约中定义")}, "B")
    _wire(monkeypatch, a, b)
    out = _run(_state(ids=("st-1",)))
    assert out["adversarial_verify_passed"] is False, "单人抓到即真——B 抓到的盲区必须打回"
    assert "st-1" in out["failed_subtask_ids"]


# ───────────────────────── 门槛 / 跳过（省成本 + 不静默） ─────────────────────────

def test_killswitch_off_skips_no_llm(monkeypatch):
    """泄压阀 SWARM_ADVERSARIAL_VERIFY=0 → 跳过（passed=None），零 LLM 花费。"""
    monkeypatch.setenv("SWARM_ADVERSARIAL_VERIFY", "0")
    a = _FakeReviewer({}, "A")
    b = _FakeReviewer({}, "B")
    _wire(monkeypatch, a, b)
    out = _run(_state())
    assert out["adversarial_verify_passed"] is None
    assert a.calls == 0 and b.calls == 0


def test_low_complexity_skips_no_llm(monkeypatch):
    """低复杂度（SIMPLE/MEDIUM）跳过——跨模块幻觉在这层不发生，省成本。"""
    a = _FakeReviewer({}, "A")
    b = _FakeReviewer({}, "B")
    _wire(monkeypatch, a, b)
    out = _run(_state(complexity=Complexity.SIMPLE))
    assert out["adversarial_verify_passed"] is None
    assert a.calls == 0


def test_partial_fuse_path_skips_no_llm(monkeypatch):
    """PARTIAL 熔断路径（dispatch_remaining 非空，after_monitor 走 #R13-4 转 merge）→
    只在干净全完成路径主动复核，此处跳过不扰动 PARTIAL 交付。"""
    a = _FakeReviewer({}, "A")
    b = _FakeReviewer({}, "B")
    _wire(monkeypatch, a, b)
    out = _run(_state(dispatch_remaining=["st-9"]))
    assert out["adversarial_verify_passed"] is None
    assert a.calls == 0


def test_audit_and_empty_diff_not_reviewed(monkeypatch):
    """AUDIT 意图 / 空 diff 子任务不进候选（无变更可复核），只审有 diff 的实现类。"""
    plan = TaskPlan(subtasks=[
        SubTask(id="st-a", description="审计", intent=TaskIntent.AUDIT,
                scope=FileScope(allow_any=True)),
        SubTask(id="st-2", description="实现", scope=FileScope(writable=["s2.x"]),
                covers=["req-2"]),
    ])
    sr = {
        "st-a": WorkerOutput(subtask_id="st-a", diff="", summary="审计", l1_passed=True),
        "st-2": _wo("st-2"),
    }
    st = {"complexity": Complexity.COMPLEX, "plan": plan, "subtask_results": sr,
          "dispatch_remaining": [], "failed_subtask_ids": []}
    a = _FakeReviewer({"st-2": ("PASS", "")}, "A")
    b = _FakeReviewer({"st-2": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    out = _run(st)
    assert "st-a" not in str(a.last_prompt), "审计/空 diff 不进复核载荷"
    assert "st-2" in str(a.last_prompt)
    assert out["adversarial_verify_passed"] is True


def test_no_candidates_passes_without_llm(monkeypatch):
    """全部候选被排除（只有审计子任务）→ 无可审即通过，零 LLM。"""
    plan = TaskPlan(subtasks=[
        SubTask(id="st-a", description="审计", intent=TaskIntent.AUDIT,
                scope=FileScope(allow_any=True)),
    ])
    sr = {"st-a": WorkerOutput(subtask_id="st-a", diff="", summary="审计", l1_passed=True)}
    st = {"complexity": Complexity.COMPLEX, "plan": plan, "subtask_results": sr,
          "dispatch_remaining": [], "failed_subtask_ids": []}
    a = _FakeReviewer({}, "A")
    b = _FakeReviewer({}, "B")
    _wire(monkeypatch, a, b)
    out = _run(st)
    assert a.calls == 0
    assert out["adversarial_verify_passed"] is True


def test_already_verified_not_rereviewed(monkeypatch):
    """已过复核【且内容未变】的子任务（token 命中）不重审——省成本，载荷只含新候选。"""
    from swarm.brain.nodes.adversarial import _verified_token
    st = _state()
    tok1 = _verified_token("st-1", st["subtask_results"]["st-1"])  # 内容绑定 token
    st["adversarial_verified_ids"] = [tok1]
    a = _FakeReviewer({"st-2": ("PASS", "")}, "A")
    b = _FakeReviewer({"st-2": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    out = _run(st)
    assert "st-1" not in str(a.last_prompt), "内容未变的已复核子任务不再进载荷"
    assert "st-2" in str(a.last_prompt)
    tok2 = _verified_token("st-2", st["subtask_results"]["st-2"])
    assert set(out["adversarial_verified_ids"]) == {tok1, tok2}


def test_regenerated_diff_rereviewed_content_bound(monkeypatch):
    """python-reviewer CONFIRMED：已复核子任务的 diff 被 rebase/重生成（内容变）→ token 失配
    → 必须【重新复核】，绝不因 id 仍在 verified 而放行未复核的新码。"""
    from swarm.brain.nodes.adversarial import _verified_token
    old_wo = _wo("st-1", diff="+ 第一版产出\n")
    stale_token = _verified_token("st-1", old_wo)          # 复核过【旧】内容
    st = _state(ids=("st-1",))
    st["subtask_results"]["st-1"] = _wo("st-1", diff="+ 重生成的新产出（可能含新幻觉）\n")  # 内容已变
    st["adversarial_verified_ids"] = [stale_token]
    a = _FakeReviewer({"st-1": ("PASS", "")}, "A")
    b = _FakeReviewer({"st-1": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    out = _run(st)
    assert a.calls == 1, "diff 内容变 → 必须重新复核（不能按旧 token 跳过）"
    assert "st-1" in str(a.last_prompt)
    assert out["adversarial_verify_passed"] is True


# ───────────────────────── 有界收敛 / 降级可观测 ─────────────────────────

def test_round_cap_escalates_not_infinite(monkeypatch):
    """MAX_ROUNDS 达上限 → 短路 escalate：不再 flag（不无限循环）、零 LLM、degraded 可观测。"""
    monkeypatch.setenv("SWARM_ADVERSARIAL_MAX_ROUNDS", "2")
    a = _FakeReviewer({"st-1": ("FAIL", "真问题 X 导致崩溃 Y")}, "A")
    b = _FakeReviewer({"st-1": ("FAIL", "真问题 X 导致崩溃 Y")}, "B")
    _wire(monkeypatch, a, b)
    out = _run(_state(ids=("st-1",), adversarial_verify_round=2))
    assert out["adversarial_verify_passed"] is None, "不收敛升人工=None（非 False 再打回）"
    assert "st-1" not in out.get("failed_subtask_ids", [])
    assert a.calls == 0, "达上限短路，绝不再烧 reviewer token"
    assert any("adversarial" in r.lower() for r in out.get("degraded_reasons", []))


def test_reviewer_infra_failure_degrades_not_blocks(monkeypatch):
    """双 reviewer 基建全挂 → 无 verdict：降级放行（passed=None）+ degraded，绝不因坏
    reviewer 黑洞掉整条交付，也不误 flag 子任务。"""
    _wire(monkeypatch, _DeadReviewer(), _DeadReviewer())
    out = _run(_state())
    assert out["adversarial_verify_passed"] is None
    assert not out.get("failed_subtask_ids")
    assert any("reviewer" in r.lower() for r in out.get("degraded_reasons", []))


def test_no_fallback_single_reviewer_degraded(monkeypatch):
    """无备用模型（fallback==primary→None）→ 退化单 reviewer（省成本不重跑同模型），
    独立性降低须 degraded 可观测；单 reviewer PASS 仍放行。"""
    a = _FakeReviewer({"st-1": ("PASS", "")}, "A")
    _wire(monkeypatch, a, None)
    out = _run(_state(ids=("st-1",)))
    assert a.calls == 1
    assert out["adversarial_verify_passed"] is True
    assert any(("single" in r.lower() or "diversity" in r.lower() or "单" in r)
               for r in out.get("degraded_reasons", []))


def test_single_reviewer_fail_still_flags(monkeypatch):
    """无备用时单 reviewer 抓到带凭据的 FAIL → 仍打回（单人抓到即真）。"""
    a = _FakeReviewer({"st-1": ("FAIL", "捏造符号 Z 编译链接失败")}, "A")
    _wire(monkeypatch, a, None)
    out = _run(_state(ids=("st-1",)))
    assert out["adversarial_verify_passed"] is False
    assert "st-1" in out["failed_subtask_ids"]


# ───────────────────────── 复核整改：静默放行面（hunter F2/F5 + F1） ─────────────────────────

def test_unreviewed_candidate_not_silently_passed(monkeypatch):
    """hunter F2：某候选无【任何】reviewer 合法 verdict（LLM 漏审）→ 绝不当 PASS+verified，
    不入 verified_ids（下轮重审）、记 incomplete_coverage degraded（挡 L6 假学习）。"""
    from swarm.brain.nodes.adversarial import _verified_token
    # 两 reviewer 都只给了 st-1 的 verdict，漏了 st-2
    a = _FakeReviewer({"st-1": ("PASS", "")}, "A")
    b = _FakeReviewer({"st-1": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    st = _state(ids=("st-1", "st-2"))
    out = _run(st)
    tok2 = _verified_token("st-2", st["subtask_results"]["st-2"])
    assert tok2 not in out["adversarial_verified_ids"], "漏审子任务绝不进 verified（否则永久跳过）"
    assert any("incomplete_coverage" in r for r in out.get("degraded_reasons", [])), \
        "漏审须记 degraded 挡 L6 假学习"
    assert "st-2" not in out.get("failed_subtask_ids", []), "漏审无负面证据，不误 flag 打回"


def test_malformed_verdict_not_treated_as_pass(monkeypatch):
    """hunter F5：畸形 verdict（非 PASS/FAIL 串）绝不当 PASS 静默放行。此处两 reviewer 都只产
    畸形 verdict → 各自零合法 verdict = 不可用 → 降级放行（passed=None），子任务不进 verified。"""
    from swarm.brain.nodes.adversarial import _verified_token
    a = _FakeReviewer({"st-1": ("MAYBE", "")}, "A")   # 非法 verdict
    b = _FakeReviewer({"st-1": ("???", "")}, "B")
    _wire(monkeypatch, a, b)
    st = _state(ids=("st-1",))
    out = _run(st)
    tok = _verified_token("st-1", st["subtask_results"]["st-1"])
    assert out["adversarial_verify_passed"] is None, "全畸形=无有效复核，降级而非误 PASS"
    assert tok not in (out.get("adversarial_verified_ids") or []), "畸形 verdict 不算 PASS、不进 verified"
    assert out.get("degraded_reasons"), "无有效复核须 degraded 可观测"


def test_partial_coverage_one_reviewer_omits_candidate(monkeypatch):
    """hunter F2 正例：reviewer 可用但漏审某候选（只审了 st-1，漏 st-2）→ st-2 落
    incomplete_coverage，不进 verified、不误 flag、passed 仍 True（无负面证据）。"""
    from swarm.brain.nodes.adversarial import _verified_token
    a = _FakeReviewer({"st-1": ("PASS", ""), "st-2": ("PASS", "")}, "A")
    b = _FakeReviewer({"st-1": ("PASS", "")}, "B")   # B 漏了 st-2（但 A 审了）→ st-2 仍算已审
    _wire(monkeypatch, a, b)
    st = _state(ids=("st-1", "st-2"))
    out = _run(st)
    # st-2 被 A 审过（PASS）→ 属已审、入 verified；不触发 incomplete（≥1 reviewer 给了 verdict）
    tok2 = _verified_token("st-2", st["subtask_results"]["st-2"])
    assert tok2 in out["adversarial_verified_ids"]
    assert out["adversarial_verify_passed"] is True


def test_unconverged_hard_blocks_auto_accept_delivery():
    """hunter F1：对抗复核 unconverged degraded → can_auto_accept_delivery 硬拦（非静默 ACCEPT）。"""
    from swarm.brain.gates import can_auto_accept_delivery
    base = {  # 其余验证面全过，只有对抗复核不收敛
        "l2_passed": True, "l3_passed": True, "runtime_smoke_passed": True,
        "acceptance_passed": True, "failed_subtask_ids": [],
        "degraded_reasons": ["adversarial_verify_unconverged:round_cap_2"],
    }
    allow, reason = can_auto_accept_delivery(base)
    assert allow is False, "对抗复核不收敛必须硬拦 auto_accept 交人工"
    assert "adversarial_verify_unconverged" in reason


def test_reviewer_unavailable_does_not_hard_block_delivery():
    """F1 分型：reviewer 不可用（缺复核、无负面证据）→ 不硬拦交付（对齐 runtime skip 哲学，
    防 provider 挂时 strand 全部交付）；挡 L6 由 should_write_success/blocking_degraded 负责。"""
    from swarm.brain.gates import can_auto_accept_delivery
    base = {
        "l2_passed": True, "l3_passed": True, "runtime_smoke_passed": True,
        "acceptance_passed": True, "failed_subtask_ids": [],
        "degraded_reasons": ["adversarial_verify_skipped:reviewer_unavailable"],
    }
    allow, _ = can_auto_accept_delivery(base)
    assert allow is True, "缺复核不硬拦交付（避免 provider 故障 strand 全部交付）"


def test_reviewer_unavailable_blocks_l6_success_learning():
    """F1 分型旁证：reviewer 不可用/漏审 degraded 经 blocking_degraded_reasons 挡 L6 假学习。"""
    from swarm.memory.pattern_extractor import blocking_degraded_reasons
    for reason in ("adversarial_verify_skipped:reviewer_unavailable",
                   "adversarial_verify_incomplete_coverage:st-2",
                   "adversarial_verify_unconverged:round_cap_2"):
        assert blocking_degraded_reasons([reason]) == [reason], \
            f"{reason} 必须阻断 L6 成功学习（非信息性白名单）"


# ───────────────────────── graph 路由级 ─────────────────────────

def test_after_adversarial_verify_three_states():
    from swarm.brain.graph import after_adversarial_verify
    assert after_adversarial_verify({"adversarial_verify_passed": False}) == "handle_failure"
    assert after_adversarial_verify({"adversarial_verify_passed": True}) == "merge"
    assert after_adversarial_verify({"adversarial_verify_passed": None}) == "merge"
    assert after_adversarial_verify({}) == "merge", "旧 checkpoint 无键=放行，不误杀"


def test_monitor_merge_label_targets_adversarial_verify():
    """after_monitor 的 'merge' 标签重指 adversarial_verify 节点（不改 after_monitor 语义，
    保 test_after_monitor_dispatch_fuse 绿）。"""
    from swarm.brain.graph import build_brain_graph
    graph = build_brain_graph()
    label_targets: dict[str, str] = {}
    for spec in graph.branches["monitor"].values():
        label_targets.update(spec.ends or {})
    assert label_targets["merge"] == "adversarial_verify", \
        "MONITOR 全完成态（标签 merge）须先经对抗验证节点"
    assert label_targets["handle_failure"] == "handle_failure"
    assert label_targets["dispatch"] == "dispatch"


def test_adversarial_verify_no_static_edge_fanout():
    """confirm fan-out 血案同款拓扑断言：adversarial_verify 出口只由条件边决定。"""
    from swarm.brain.graph import build_brain_graph
    graph = build_brain_graph()
    assert "adversarial_verify" in graph.nodes
    static_targets = {dst for (src, dst) in graph.edges if src == "adversarial_verify"}
    assert static_targets == set(), \
        f"adversarial_verify 出现静态边 {static_targets}：与条件边并存会 fan-out 并行触发"
    assert "adversarial_verify" in graph.branches
    ends = set()
    for spec in graph.branches["adversarial_verify"].values():
        ends.update((spec.ends or {}).values())
    assert ends == {"merge", "handle_failure"}, f"条件边出口不符: {ends}"


# ───────────────────────── R53-4：机制 provenance + 粘滞熔断（round49b/50b/51/52 实锤） ────────

def test_r53_4_mechanism_paths_are_disclosed_and_exempted(monkeypatch):
    """★头号锁★ 确定性修复层的产物必须在载荷里标注免责——否则 reviewer 结构上无法区分，
    必然把 L1 注入的版本号指控成"worker 擅自硬编码"，worker 删 → 机制再注入 → 死循环。"""
    a = _FakeReviewer({"st-1": ("PASS", "")}, "A")
    b = _FakeReviewer({"st-1": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    st = _state(ids=("st-1",))
    st["subtask_results"]["st-1"] = WorkerOutput(
        subtask_id="st-1", diff="+ code\n", summary="s", l1_passed=True,
        l1_details={"repaired_file_paths": ["pom.xml", "alarm-api/pom.xml"],
                    "modified_files": ["alarm-api/src/A.java", "alarm-api/pom.xml"]})
    _run(st)
    prompt = "\n".join(str(m.get("content", "")) for m in a.last_prompt)
    assert "不计 worker 的账" in prompt
    assert "alarm-api/pom.xml" in prompt and "pom.xml" in prompt
    assert "实际改动文件清单" in prompt and "alarm-api/src/A.java" in prompt


def test_r53_4_file_list_never_truncated_even_when_diff_is(monkeypatch):
    """diff 截断不得让 reviewer 误判"未创建任何文件"（round50b 冤杀）：文件清单永不截断。"""
    monkeypatch.setenv("SWARM_ADVERSARIAL_DIFF_CHARS", "200")
    a = _FakeReviewer({"st-1": ("PASS", "")}, "A")
    b = _FakeReviewer({"st-1": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    st = _state(ids=("st-1",))
    st["subtask_results"]["st-1"] = WorkerOutput(
        subtask_id="st-1", diff="+ x\n" * 500, summary="s", l1_passed=True,
        l1_details={"modified_files": [f"m/F{i}.java" for i in range(8)]})
    _run(st)
    prompt = "\n".join(str(m.get("content", "")) for m in a.last_prompt)
    assert "已截断" in prompt, "前置：diff 确实被截断"
    for i in range(8):
        assert f"m/F{i}.java" in prompt, "文件清单必须完整进载荷"


def test_r53_4_escalated_task_never_flags_back_again(monkeypatch):
    """★粘滞熔断★ 已宣布"未收敛→升人工（绝不再打回）"后，rebase 重派再复核只出 advisory。

    round50b/52 实锤：cap 短路把 round 归零，rebase 重派后计数从 0 起 → 又连打回 3 轮，
    把已 L1 通过的产出反复打成失败 → 连坐放弃 → 空转到用户取消。
    """
    a = _FakeReviewer({"st-1": ("FAIL", "NPE：调用了不存在的 Foo.bar()")}, "A")
    b = _FakeReviewer({"st-1": ("PASS", "")}, "B")
    _wire(monkeypatch, a, b)
    st = _state(ids=("st-1",),
                degraded_reasons=["adversarial_verify_unconverged:round_cap_2"])
    out = _run(st)
    assert out["adversarial_verify_passed"] is not False, "熔断后绝不再打回"
    assert not out.get("failed_subtask_ids"), "不得再进失败集（那会触发连坐放弃）"
    assert st["subtask_results"]["st-1"].l1_passed is True, "已通过的产出不得被改写成失败"
    assert any("advisory" in r for r in out.get("degraded_reasons", [])), "发现仍留痕（不静默）"


def test_r53_4_runtime_reviewer_death_is_recorded_as_degraded(monkeypatch):
    """reviewer 运行时挂掉 → 仍出裁决，但独立性丢失必须留痕（此前完全不记账，人工面看不见）。"""
    a = _FakeReviewer({"st-1": ("PASS", "")}, "A")
    _wire(monkeypatch, a, _DeadReviewer())
    st = _state(ids=("st-1",))
    out = _run(st)
    assert any("reviewer_died_at_runtime" in r for r in out.get("degraded_reasons", []))
