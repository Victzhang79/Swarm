"""R65E-T3（效率治本，round65e4 Agent A 实测 ~22min 浪费）：elaborate 二次拆分（LLM，每轮
~11min）绝改不了模块 coherence 违例（只在模块内拆子任务、不动 module/file_plan 归属）。plan 已带
G1 硬违例时 validate_plan 必打回本轮全量重产——resplit 是注定被丢弃的烧钱空转，跳过之。

soundness：只跳【昂贵 LLM 环】，确定性尾段照跑、validate_plan 仍为唯一权威；用【与 G1 闸同源同
输入】的 validate_module_coherence 预检，gate 关闭时不跳（严格与闸同步）。
"""
import asyncio

import swarm.brain.planning_nodes as P
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _oversized_sub(sid="st-1"):
    budget = P._context_budget()
    return SubTask(
        id=sid, description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[], readable=[]),
        acceptance_criteria=["x"], est_context_tokens=budget + 50_000, depends_on=[],
    )


# 会硬判 G1 违① 的 tech_design_file_plan：模块 mixed 的真 Java 跨两物理构建单元
_DOOMED_FP = [
    {"module": "mixed", "path": "mod-a/src/main/java/com/x/A.java"},
    {"module": "mixed", "path": "mod-b/src/main/java/com/x/B.java"},
]
# 相干的 file_plan：模块 solo 的 Java 全在单一构建单元
_OK_FP = [
    {"module": "solo", "path": "solo/src/main/java/com/x/A.java"},
    {"module": "solo", "path": "solo/src/main/java/com/x/B.java"},
]


def _mock_llm_splitter():
    class _R:
        content = ('{"subtasks":[{"description":"part A","acceptance_criteria":["a"],"est_context_tokens":40000},'
                   '{"description":"part B","acceptance_criteria":["b"],"est_context_tokens":40000}]}')

    class _L:
        async def ainvoke(self, m):
            return _R()
    return _L()


def _run(state):
    _orig = P._get_brain_llm
    P._get_brain_llm = lambda: _mock_llm_splitter()
    try:
        return asyncio.run(P.elaborate(state))
    finally:
        P._get_brain_llm = _orig


def test_doomed_coherence_skips_resplit(monkeypatch):
    """plan 带 G1 硬违例 → 跳过 resplit：超预算子任务【不】被二次拆分（省 LLM 重产）。"""
    monkeypatch.setenv("SWARM_MODULE_COHERENCE_GATE", "1")
    plan = TaskPlan(subtasks=[_oversized_sub()], parallel_groups=[["st-1"]])
    out = _run({"plan": plan, "task_id": "", "project_id": "", "tech_design_file_plan": _DOOMED_FP})
    # 行为断言（非实现细节）：token-超预算的 st-1 未被 LLM 二次拆分 → 仍是 1 个子任务（mock 会拆成 2）。
    eff = out.get("plan") or plan
    assert len(eff.subtasks) == 1, f"doomed plan 应跳过 LLM resplit、st-1 不拆；实得 {len(eff.subtasks)} 个"
    assert "st-1" in (out.get("oversized_subtask_ids") or []), (
        "跳过 LLM resplit 后 token 超预算子任务仍应被如实标记 oversized（可观测不丢）")


def test_coherent_plan_still_resplits(monkeypatch):
    """相干 plan（无 G1 违例）→ R65E-T3 不触发：超预算子任务照常二次拆分（不过度抑制）。"""
    monkeypatch.setenv("SWARM_MODULE_COHERENCE_GATE", "1")
    plan = TaskPlan(subtasks=[_oversized_sub()], parallel_groups=[["st-1"]])
    out = _run({"plan": plan, "task_id": "", "project_id": "", "tech_design_file_plan": _OK_FP})
    new_plan = out.get("plan")
    assert new_plan is not None and len(new_plan.subtasks) == 2, (
        f"相干 plan 的超预算子任务应照常拆成 2 个；out.plan={new_plan}")


def test_cands_channel_violation_does_not_skip(monkeypatch):
    """★复核 HIGH 整改回归锁★ 违例来自【subtask scope cands 通道】而 file_plan 通道干净时，绝不
    跳 resplit——因为尾段 normalize Rule-0 可能把幻觉 writable 重定位、令 cands invalid→valid 翻转，
    据它抢跳=对尾段会自愈的好 plan 误跳、超预算子任务未拆直奔 dispatch。预检用【空 subtasks 探针】
    只看 file_plan 通道，故此处不跳、超预算子任务照常二次拆分。"""
    monkeypatch.setenv("SWARM_MODULE_COHERENCE_GATE", "1")
    # 契约声明模块 core；两子任务把 core 落到两个物理目录 → cands 通道违① invalid（plan_obj 层）。
    # 但 file_plan（tech_design_file_plan）为空/干净 → 空探针 verdict 不受 cands 影响 → 不跳。
    contract = {"dependencies": [{"module": "core"}]}
    over = SubTask(
        id="st-1", description="oversized multi-file", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["svc-a/core/src/main/java/A.java",
                                  "svc-a/core/src/main/java/C.java"], readable=[]),
        acceptance_criteria=["x"], est_context_tokens=P._context_budget() + 50_000, depends_on=[])
    other = SubTask(
        id="st-2", description="second dir for cands violation", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["svc-b/core/src/main/java/B.java"], readable=[]),
        acceptance_criteria=["y"], est_context_tokens=1000, depends_on=[])
    plan = TaskPlan(subtasks=[over, other], parallel_groups=[["st-1", "st-2"]], shared_contract=contract)
    out = _run({"plan": plan, "task_id": "", "project_id": "", "tech_design_file_plan": []})
    assert out.get("plan") is not None, (
        "cands 通道违例（file_plan 干净）不得触发跳过——超预算子任务应照常二次拆分（防误跳好 plan）")


def test_gate_off_restores_resplit(monkeypatch):
    """SWARM_MODULE_COHERENCE_GATE=0：validate_plan 不据 coherence 打回 → 本预检亦不跳（严格与闸
    同步），超预算子任务照常拆分——绝不据一个被运维关掉的判据抢跳。"""
    monkeypatch.setenv("SWARM_MODULE_COHERENCE_GATE", "0")
    plan = TaskPlan(subtasks=[_oversized_sub()], parallel_groups=[["st-1"]])
    out = _run({"plan": plan, "task_id": "", "project_id": "", "tech_design_file_plan": _DOOMED_FP})
    new_plan = out.get("plan")
    assert new_plan is not None and len(new_plan.subtasks) == 2, (
        f"闸关时不得抢跳 resplit，超预算子任务应照常拆分；out.plan={new_plan}")
