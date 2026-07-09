"""阶段3.3 A12（登记册 §二）：批间人造串行链 → 只按真实 module_deps 连边。

病理：merge_subtask_batches 把每批首任务强制依赖前批末任务——并行度塌缩≈1；
早批放弃沿链连坐全部后续模块；elaborate 的假依赖剥离对这条边行为不可预测。
治本：调用方传 batch_modules+module_deps → 跨批边只按真实模块依赖连；同 base 模块
的容量子批（mod#i/k）/bisect 半批（mod~a）保持模块内串行（既有安全语义）；
不传模块信息=legacy 串行门控不变（零回归）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from swarm.brain.plan_batch import merge_subtask_batches


def _b(*descs):
    return [{"id": f"x{i}", "description": d} for i, d in enumerate(descs, 1)]


def _dep_ids(merged):
    return {m["id"]: set(m.get("depends_on") or []) for m in merged}


# ─────────────── 纯函数：true-deps 模式 ───────────────

def test_independent_modules_have_no_cross_batch_edges():
    merged = merge_subtask_batches(
        [_b("a1", "a2"), _b("b1"), _b("c1")],
        batch_modules=["modA", "modB", "modC"],
        module_deps={},
    )
    deps = _dep_ids(merged)
    ids_a = {merged[0]["id"], merged[1]["id"]}
    assert not deps[merged[2]["id"]] & ids_a, "无真实依赖的模块间绝不连边（并行度不塌缩）"
    assert not deps[merged[3]["id"]] & (ids_a | {merged[2]["id"]}), (
        f"modC 独立却被串行门控: {merged[3]}")


def test_true_module_dep_connects_first_to_dep_last():
    merged = merge_subtask_batches(
        [_b("a1", "a2"), _b("b1"), _b("c1")],
        batch_modules=["modA", "modB", "modC"],
        module_deps={"modB": ["modA"]},
    )
    deps = _dep_ids(merged)
    a_last = merged[1]["id"]
    b_first = merged[2]["id"]
    assert a_last in deps[b_first], "真实 module_deps 必须连边（B 依赖 A 的末任务）"
    assert not deps[merged[3]["id"]], "modC 无依赖=零边"


def test_same_base_module_subbatches_stay_serial():
    """容量切分 mod#i/k 与 bisect 半批 mod~a 同 base 模块——模块内保持串行（既有安全语义）。"""
    merged = merge_subtask_batches(
        [_b("p1"), _b("p2"), _b("q1")],
        batch_modules=["modP#1/2", "modP#2/2", "modQ~a"],
        module_deps={},
    )
    deps = _dep_ids(merged)
    assert merged[0]["id"] in deps[merged[1]["id"]], "同模块子批必须保持模块内串行"
    assert not deps[merged[2]["id"]], "异模块 bisect 半批不受串行链牵连"


def test_dep_on_failed_module_batch_is_skipped():
    """依赖的模块整批失败（无子任务产出）→ 无边可连（不悬空、不连坐）。"""
    merged = merge_subtask_batches(
        [_b("b1")],
        batch_modules=["modB"],
        module_deps={"modB": ["modA"]},  # modA 批失败未进 batch_results
    )
    assert not _dep_ids(merged)[merged[0]["id"]]


def test_legacy_serial_chain_preserved_without_module_info():
    """护栏：不传模块信息=legacy 串行门控逐字节不变（零回归）。"""
    merged = merge_subtask_batches([_b("a1", "a2"), _b("b1")])
    assert merged[1]["id"] in _dep_ids(merged)[merged[2]["id"]]


# ─────────────── 调用点接线：_plan_ultra_batched ───────────────

_ST_A = {"subtasks": [{"id": "st-1", "description": "实现 alarm-sdk",
                       "scope": {"create_files": ["alarm-sdk/src/A.java"],
                                 "writable": [], "readable": []}}]}
_ST_B = {"subtasks": [{"id": "st-1", "description": "实现 system-enhance",
                       "scope": {"create_files": ["system-enhance/src/B.java"],
                                 "writable": [], "readable": []}}]}


class _R:
    def __init__(self, content):
        self.content = content


class _TwoModLLM:
    async def ainvoke(self, msgs):
        user = msgs[-1]["content"]
        if "'alarm-sdk'" in user:
            return _R(json.dumps(_ST_A, ensure_ascii=False))
        return _R(json.dumps(_ST_B, ensure_ascii=False))


async def test_ultra_batched_no_artificial_serial_between_independent_modules(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "5")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    import swarm.brain.nodes as _nodes
    monkeypatch.setattr(_nodes, "_get_brain_fallback_llm", lambda: None)
    from swarm.brain.nodes import _plan_ultra_batched
    state = {
        "tech_design": {"modules": [
            {"name": "alarm-sdk", "depends_on": []},
            {"name": "system-enhance", "depends_on": []},
        ]},
        "shared_contract_draft": {},
        "project_id": "",
    }
    file_plan = [
        {"path": "alarm-sdk/src/A.java", "module": "alarm-sdk", "action": "create"},
        {"path": "system-enhance/src/B.java", "module": "system-enhance", "action": "create"},
    ]
    plan, failed, _bl, _c = await _plan_ultra_batched(
        _TwoModLLM(), state, "需求", {}, "", file_plan)
    assert not failed
    by_desc = {("alarm" if "alarm" in st.description else "enh"): st
               for st in plan.subtasks}
    assert len(plan.subtasks) == 2
    assert not (set(by_desc["enh"].depends_on or [])
                & {by_desc["alarm"].id}), (
        "独立模块（depends_on=[]）之间绝不能有人造串行边（A12）")


async def test_ultra_batched_true_dep_edge_survives(monkeypatch):
    """有真实依赖（system-enhance 依赖 alarm-sdk）→ 边必须在。"""
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "5")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    import swarm.brain.nodes as _nodes
    monkeypatch.setattr(_nodes, "_get_brain_fallback_llm", lambda: None)
    from swarm.brain.nodes import _plan_ultra_batched
    state = {
        "tech_design": {"modules": [
            {"name": "alarm-sdk", "depends_on": []},
            {"name": "system-enhance", "depends_on": ["alarm-sdk"]},
        ]},
        "shared_contract_draft": {},
        "project_id": "",
    }
    file_plan = [
        {"path": "alarm-sdk/src/A.java", "module": "alarm-sdk", "action": "create"},
        {"path": "system-enhance/src/B.java", "module": "system-enhance", "action": "create"},
    ]
    plan, failed, _bl, _c = await _plan_ultra_batched(
        _TwoModLLM(), state, "需求", {}, "", file_plan)
    assert not failed
    by_desc = {("alarm" if "alarm" in st.description else "enh"): st
               for st in plan.subtasks}
    assert by_desc["alarm"].id in (by_desc["enh"].depends_on or []), (
        "真实 module_deps 的批间边必须保留")
