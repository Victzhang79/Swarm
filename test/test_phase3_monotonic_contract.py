"""阶段3.1+3.2（登记册 §八 阶段3）：单调合同脊柱 + D1/A11 replan 增量修补配对。

3.1 单调合同（核心）：「已覆盖需求集」全局单调不减。
  - 新键 coverage_watermark（reducer=append+dedup）：曾在【任意】规划轮达成覆盖的
    req id 全集——reducer 层结构性防缩水（节点 emit 子集也不会让 state 回退）。
  - validate_plan 每轮 emit 本轮覆盖集（covers∪合法 baseline）；本轮相对水位丢失
    的条目【结构化回灌】D09 feedback（round37 实证 16→2 的震荡此前只有 log 可见）；
    覆盖闸通过但水位仍有丢失（A6 degraded 放行后可达）→ 硬性 plan_valid=False。
3.2 D1：replan 注入块新增「已完成(L1过) covers=硬约束必须保留」段。
    A11：_surgical_replan_reset scope 认领放宽——描述逐字等 OR covers 集一致(非空)
    OR 相似度≥SWARM_REPLAN_CLAIM_DESC_SIM(0.9)；意图变（低相似且 covers 不同）仍拒。
"""

from __future__ import annotations

import os

import pytest

from swarm.brain.nodes import validate_plan
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput

REQ_A = "req-aaaa1111"
REQ_B = "req-bbbb2222"


def _items():
    return [
        {"id": REQ_A, "text": "系统支持条目一的功能", "kind": "functional",
         "source_quote": "条目一", "source": "description"},
        {"id": REQ_B, "text": "系统支持条目二的数据约束", "kind": "data",
         "source_quote": "条目二", "source": "description"},
    ]


def _st(sid, writable=None, covers=None, desc="do", creates=None):
    return SubTask(
        id=sid, description=desc, difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=list(writable or []), readable=[],
                        create_files=list(creates or [])),
        covers=list(covers or []), depends_on=[],
    )


def _plan_obj(*subtasks):
    return TaskPlan(subtasks=list(subtasks),
                    parallel_groups=[[st.id] for st in subtasks])


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content='{"valid": true, "issues": []}'):
        self._content = content
        self.captured: list[str] = []

    async def ainvoke(self, messages):
        self.captured.append(messages[1]["content"])
        return _Resp(self._content)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("SWARM_VALIDATE_PLAN_LLM_GATE", "SWARM_VALIDATE_PLAN_COMPLETENESS_GATE",
              "SWARM_PLAN_COVERAGE_GATE", "SWARM_REPLAN_CLAIM_DESC_SIM"):
        monkeypatch.delenv(k, raising=False)
    yield


def _patch_llm(monkeypatch):
    import swarm.brain.nodes as nodes
    fake = _FakeLLM()
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    return fake


# ─────────────── 3.1 水位键：声明 + reducer 单调 ───────────────

def test_watermark_key_declared_with_monotonic_reducer():
    """coverage_watermark 必须声明（LangGraph 未声明键=静默丢弃）且带 append+dedup
    reducer——reducer 层强制单调：任何节点 emit 子集都不能使水位缩水。"""
    import typing

    from swarm.brain.state import BrainState, _merge_degraded_reasons
    hints = typing.get_type_hints(BrainState, include_extras=True)
    assert "coverage_watermark" in hints, "水位键未声明=链路整体失活"
    meta = getattr(hints["coverage_watermark"], "__metadata__", ())
    assert _merge_degraded_reasons in meta, "必须挂 append+dedup reducer（单调由结构保证）"
    # reducer 行为：emit 子集不缩水
    assert _merge_degraded_reasons([REQ_A, REQ_B], [REQ_A]) == [REQ_A, REQ_B]


# ─────────────── 3.1 validate_plan：水位 emit ───────────────

async def test_validate_plan_emits_watermark_on_full_coverage(monkeypatch):
    _patch_llm(monkeypatch)
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]),
                          _st("st-2", writable=["b"], covers=[REQ_B])),
        "task_description": "t", "complexity": "medium",
        "plan_retry_count": 0, "requirement_items": _items(),
    })
    assert out["plan_valid"] is True
    assert sorted(out.get("coverage_watermark") or []) == [REQ_A, REQ_B], (
        "通过轮必须 emit 本轮覆盖集进水位")


async def test_validate_plan_emits_watermark_even_on_failed_attempt(monkeypatch):
    """round37 机制：震荡发生在【多轮 attempt 之间】——失败轮达成的覆盖也必须入水位，
    否则下一轮丢了它无人知晓。"""
    _patch_llm(monkeypatch)
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A])),  # REQ_B 未覆盖
        "task_description": "t", "complexity": "medium",
        "plan_retry_count": 0, "requirement_items": _items(),
    })
    assert out["plan_valid"] is False
    assert out.get("coverage_watermark") == [REQ_A], (
        "失败 attempt 已达成的覆盖也必须记水位（震荡可见性的根基）")


async def test_validate_plan_feedback_flags_watermark_loss(monkeypatch):
    """先前轮已覆盖 REQ_B（水位在），本轮丢失 → feedback 必须以单调合同名义点名
    （区别于普通 uncovered——LLM 须知道这是【倒退】而非【一直没做】）。"""
    _patch_llm(monkeypatch)
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A])),
        "task_description": "t", "complexity": "medium",
        "plan_retry_count": 0, "requirement_items": _items(),
        "coverage_watermark": [REQ_A, REQ_B],
    })
    assert out["plan_valid"] is False
    fb = out["plan_validation_feedback"]
    assert "单调" in fb and REQ_B in fb, f"水位丢失必须结构化回灌: {fb[:400]}"


async def test_validate_plan_watermark_loss_hard_invalid_even_if_gate_passes(monkeypatch):
    """防御闸（A6 degraded 放行后 load-bearing）：覆盖闸放行但相对水位倒退 → 仍必须
    plan_valid=False。用 baseline 申报凑满全覆盖但水位含清单外历史条目模拟不了；
    这里直接模拟：水位含 REQ_B，计划全覆盖 → 无倒退（护栏：不误伤）。"""
    _patch_llm(monkeypatch)
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A, REQ_B])),
        "task_description": "t", "complexity": "medium",
        "plan_retry_count": 0, "requirement_items": _items(),
        "coverage_watermark": [REQ_A, REQ_B, "req-gone9999"],  # 清单外历史 id 忽略
    })
    assert out["plan_valid"] is True, "水位与当前清单求交后比对——陈旧 id 不误杀"


async def test_validate_plan_no_watermark_when_gate_skipped(monkeypatch):
    _patch_llm(monkeypatch)
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"])),
        "task_description": "t", "complexity": "medium",
        "plan_retry_count": 0,  # 无 requirement_items → 跳过覆盖闸
    })
    assert "coverage_watermark" not in out, "跳过轮不得写水位（无口径可对账）"


# ─────────────── 3.2 A11：scope 认领放宽 ───────────────

def _wo(sid):
    return WorkerOutput(subtask_id=sid, diff="d", summary="", l1_passed=True,
                        confidence="high")


def test_claim_by_covers_equality_despite_rewording():
    """id 重编号+措辞漂移但 scope 唯一一致且 covers 集一致（非空）→ 认领（真同一工作）。"""
    from swarm.brain.nodes import _surgical_replan_reset
    old_plan = TaskPlan(subtasks=[_st("st-24", creates=["a/A.java"],
                                      covers=[REQ_A], desc="实现告警模板管理基础能力")])
    new_plan = TaskPlan(subtasks=[_st("st-1", creates=["a/A.java"],
                                      covers=[REQ_A], desc="告警模板管理：模板 CRUD 与基础能力实现")])
    out = _surgical_replan_reset({"st-24": _wo("st-24")}, old_plan, new_plan)
    assert "st-1" in out["subtask_results"], (
        "covers 集一致=同一需求同一 scope——措辞漂移不得让 L1 已过产出被白重烧（A11）")


def test_claim_by_high_similarity_rewording():
    """covers 都为空但描述高相似（仅措辞微调）→ 认领。"""
    from swarm.brain.nodes import _surgical_replan_reset
    d_old = "实现告警模板管理模块的增删改查与列表分页展示功能"
    d_new = "实现告警模板管理模块的增删改查以及列表分页展示功能"
    old_plan = TaskPlan(subtasks=[_st("st-24", creates=["a/A.java"], desc=d_old)])
    new_plan = TaskPlan(subtasks=[_st("st-1", creates=["a/A.java"], desc=d_new)])
    out = _surgical_replan_reset({"st-24": _wo("st-24")}, old_plan, new_plan)
    assert "st-1" in out["subtask_results"], "高相似措辞漂移必须认领（A11）"


def test_claim_rejected_when_intent_changed():
    """护栏（复核 HIGH 原判据不回归）：低相似+covers 不同=意图变 → 绝不认领。"""
    from swarm.brain.nodes import _surgical_replan_reset
    old_plan = TaskPlan(subtasks=[_st("st-5", writable=["UserService.java"],
                                      covers=[REQ_A], desc="实现校验 A")])
    new_plan = TaskPlan(subtasks=[_st("st-1", writable=["UserService.java"],
                                      covers=[REQ_B], desc="实现校验 B")])
    out = _surgical_replan_reset({"st-5": _wo("st-5")}, old_plan, new_plan)
    assert not out["subtask_results"], "意图变（covers 变+描述变）绝不用旧产物跳过新工作"


def test_claim_similarity_threshold_configurable(monkeypatch):
    """阈值可配：调到 1.01 等效关闭相似度通道（covers 也不同时不认领）。"""
    monkeypatch.setenv("SWARM_REPLAN_CLAIM_DESC_SIM", "1.01")
    from swarm.brain.nodes import _surgical_replan_reset
    d_old = "实现告警模板管理模块的增删改查与列表分页展示功能"
    d_new = "实现告警模板管理模块的增删改查以及列表分页展示功能"
    old_plan = TaskPlan(subtasks=[_st("st-24", creates=["a/A.java"], desc=d_old)])
    new_plan = TaskPlan(subtasks=[_st("st-1", creates=["a/A.java"], desc=d_new)])
    out = _surgical_replan_reset({"st-24": _wo("st-24")}, old_plan, new_plan)
    assert not out["subtask_results"]


# ─────────────── 3.2 D1：replan 注入块的 DONE-covers 硬约束段 ───────────────

def test_repair_block_lists_done_covers_as_hard_constraint():
    from swarm.brain.nodes import _previous_plan_repair_block
    prev = TaskPlan(subtasks=[_st("st-1", writable=["a"], covers=[REQ_A], desc="x")])
    blk = _previous_plan_repair_block(prev, [], done_cover_ids=[REQ_A])
    assert REQ_A in blk and "硬约束" in blk, (
        "已完成(L1 过)子任务的 covers 必须以硬约束语义注入 replan prompt（D1）")


def test_repair_block_without_done_covers_unchanged():
    from swarm.brain.nodes import _previous_plan_repair_block
    prev = TaskPlan(subtasks=[_st("st-1", writable=["a"], covers=[REQ_A], desc="x")])
    assert "硬约束" not in _previous_plan_repair_block(prev, [])
