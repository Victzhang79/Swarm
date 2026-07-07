"""P2-14 D51：共享契约不再 enrich 进每个子任务——派发面（build_worker_prompt）现场合成。

不变量：worker 可见契约与旧 plan 期 enrich 行为【逐字节等价】；旧 checkpoint（subtask.contract
已含 shared 副本）恢复后合成幂等；plan 节点产出的 plan 不再为每个子任务携带 shared 副本。
"""

from __future__ import annotations

import copy

from swarm.brain.contract_utils import enrich_plan_with_shared_contract
from swarm.types import FileScope, SubTask, TaskPlan
from swarm.worker.prompts import build_worker_prompt


def _mk_subtask(contract: dict | None = None) -> SubTask:
    return SubTask(
        id="st-1",
        description="demo",
        scope=FileScope(writable=["a/b.py"], readable=[]),
        contract=contract or {},
    )


_SHARED = {
    "api": "SHARED_API_MARK",
    "own_key": "shared-version",
    "dependencies": [{"group": "g", "artifact": "a"}],
}


def test_d51_prompt_contains_synthesized_full_contract():
    """raw 子任务（contract 未含 shared）→ prompt 里 subtask_contract 已合成 shared 打底。"""
    st = _mk_subtask({"own_key": "sub-version", "sub_only": 1})
    prompt = build_worker_prompt(st, shared_contract=dict(_SHARED))
    assert "SHARED_API_MARK" in prompt            # shared 键进入了子任务契约
    assert '"own_key": "sub-version"' in prompt   # 子任务字段覆盖 shared 同名键（precedence 不变）
    assert '"sub_only": 1' in prompt


def test_d51_prompt_equivalent_to_old_enrich_path():
    """新（raw + 派发合成）与旧（plan 期 enrich 后）产出的 worker prompt 逐字节一致。"""
    raw_plan = TaskPlan(subtasks=[_mk_subtask({"own_key": "sub-version"})],
                        shared_contract=dict(_SHARED))
    enriched_plan = enrich_plan_with_shared_contract(copy.deepcopy(raw_plan))

    p_new = build_worker_prompt(raw_plan.subtasks[0], shared_contract=raw_plan.shared_contract)
    p_old = build_worker_prompt(enriched_plan.subtasks[0],
                                shared_contract=enriched_plan.shared_contract)
    assert p_new == p_old


def test_d51_old_checkpoint_contract_idempotent():
    """旧 checkpoint 恢复：subtask.contract 已是 enrich 产物 → 再合成幂等，prompt 不变。"""
    enriched_once = enrich_plan_with_shared_contract(
        TaskPlan(subtasks=[_mk_subtask({"own_key": "sub-version"})], shared_contract=dict(_SHARED))
    )
    st = enriched_once.subtasks[0]
    p1 = build_worker_prompt(st, shared_contract=enriched_once.shared_contract)
    # 再走一遍（模拟重复派发/重试），仍一致
    p2 = build_worker_prompt(st, shared_contract=enriched_once.shared_contract)
    assert p1 == p2
    assert "SHARED_API_MARK" in p1


def test_d51_no_shared_contract_behaves_as_before():
    st = _mk_subtask({"only": "sub"})
    prompt = build_worker_prompt(st, shared_contract=None)
    assert '"only": "sub"' in prompt
    st_empty = _mk_subtask()
    prompt_empty = build_worker_prompt(st_empty, shared_contract=None)
    assert "（无契约约束）" in prompt_empty


def test_d51_plan_dump_no_longer_inlines_shared_per_subtask():
    """体积不变量：raw plan 的 model_dump 只含 1 份 shared（plan 级），不再 N 份内联。"""
    import json

    subtasks = [
        SubTask(id=f"st-{i}", description="d",
                scope=FileScope(writable=[f"f{i}.py"], readable=[]))
        for i in range(5)
    ]
    plan = TaskPlan(subtasks=subtasks, shared_contract=dict(_SHARED))
    dumped = json.dumps(plan.model_dump(), ensure_ascii=False)
    # 旧 enrich 后为 1(plan级) + 5(每子任务) = 6 份；现在只有 plan 级 1 份
    assert dumped.count("SHARED_API_MARK") == 1
