"""阶段3.7 F10（登记册 §六）：validate 的 LLM 软校验每轮必烧、结果默认丢弃、无外层
超时包装 → 只在首轮/结构变化时跑 + 走 _invoke_llm_abortable（流式看门狗+软硬双限）。

签名口径=子任务 (desc, writable, create_files) 集合 sha1（id 会被重编号，不进签名）。
plan_soft_review_sig 新键 last-write-wins（每轮整体替换，绝不 reducer 粘滞）。
"""

from __future__ import annotations

import pytest

from swarm.brain.nodes import validate_plan
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

REQ_A = "req-aaaa1111"


def _items():
    return [{"id": REQ_A, "text": "功能一", "kind": "functional",
             "source_quote": "一", "source": "description"}]


def _plan(desc="do", writable=("a",)):
    return TaskPlan(subtasks=[SubTask(
        id="st-1", description=desc, difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=list(writable), readable=[]), covers=[REQ_A],
        depends_on=[])], parallel_groups=[["st-1"]])


class _Resp:
    def __init__(self, content):
        self.content = content


@pytest.fixture()
def _abortable_recorder(monkeypatch):
    import swarm.brain.nodes as nodes
    calls = []

    async def _fake_abortable(llm, messages, timeout, fallback=None, node_label=""):
        calls.append({"timeout": timeout})
        return _Resp('{"valid": true, "issues": []}')

    monkeypatch.setattr(nodes, "_invoke_llm_abortable", _fake_abortable)

    class _NoDirectLLM:
        async def ainvoke(self, msgs):
            raise AssertionError("软校验必须走 _invoke_llm_abortable，不得裸 ainvoke（F10）")

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _NoDirectLLM())
    return calls


async def _run(plan, retry=0, prev_sig=None):
    st = {"plan": plan, "task_description": "t", "complexity": "medium",
          "plan_retry_count": retry, "requirement_items": _items()}
    if prev_sig is not None:
        st["plan_soft_review_sig"] = prev_sig
    return await validate_plan(st)


async def test_soft_review_runs_first_round_via_abortable(_abortable_recorder):
    out = await _run(_plan(), retry=0)
    assert out["plan_valid"] is True
    assert len(_abortable_recorder) == 1, "首轮软校验必须跑且走 abortable"
    assert out.get("plan_soft_review_sig"), "必须 emit 结构签名供后续轮比对"


async def test_soft_review_skipped_on_retry_same_structure(_abortable_recorder):
    o0 = await _run(_plan(), retry=0)
    sig = o0["plan_soft_review_sig"]
    o1 = await _run(_plan(), retry=1, prev_sig=sig)
    assert o1["plan_valid"] is True
    assert len(_abortable_recorder) == 1, (
        "重试轮结构未变必须跳过软校验（每轮必烧+结果丢弃=纯浪费，F10）")
    assert o1.get("plan_soft_review_sig") == sig


async def test_soft_review_reruns_on_structure_change(_abortable_recorder):
    o0 = await _run(_plan(), retry=0)
    o1 = await _run(_plan(desc="换了个拆法", writable=("b",)), retry=1,
                    prev_sig=o0["plan_soft_review_sig"])
    assert len(_abortable_recorder) == 2, "结构变化必须重跑软校验"
    assert o1["plan_soft_review_sig"] != o0["plan_soft_review_sig"]


def test_signature_ignores_id_churn():
    from swarm.brain.nodes import _plan_soft_signature
    p1 = _plan()
    p2 = _plan()
    p2.subtasks[0].id = "st-99"  # replan 重编号
    assert _plan_soft_signature(p1) == _plan_soft_signature(p2), (
        "id 重编号不改结构——签名不含 id")
    assert _plan_soft_signature(_plan(desc="x")) != _plan_soft_signature(p1)
