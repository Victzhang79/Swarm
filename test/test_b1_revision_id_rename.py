#!/usr/bin/env python3
"""30 号文批13 B-1 锁：revision 子任务 id 确定性撞名改写（fail-closed，零 LLM）。

原病（finding 探针实测形状 plan id=['st-1','rev-1','rev-1']）：LLM 按 REVISION_USER
示例恒返 "rev-1"，第二次 REVISE 与第一轮已完成 id 撞名 → `_is_ready`
（`task.id not in completed_ids`）恒判不就绪 → get_dispatch_batch 恒空批 →
R13-4 熔断路由 MERGE → 第二轮修订从未执行，任务却带第一轮旧 merged_diff 再交付
（人工意图被静默吞掉=假成功方向）。

治法：revision() 三条构造路径（正常/JSON 失败/异常兜底）汇合后统一过撞名闸——
与既有 id 冲突即确定性改写为第一个空闲 `rev-N` + 一次 WARNING。纯字符串判重，
宁可改名不可丢修订。
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.types import (
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


class _Resp:
    def __init__(self, content):
        self.content = content


def _fake_llm(payload: str):
    class _L:
        async def ainvoke(self, _msgs):
            return _Resp(payload)
    return lambda: _L()


def _st(sid: str) -> SubTask:
    return SubTask(id=sid, description=f"d-{sid}",
                   difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                   scope=FileScope(writable=[f"{sid}.py"]))


def _plan(*ids: str) -> TaskPlan:
    return TaskPlan(subtasks=[_st(i) for i in ids],
                    parallel_groups=[[i] for i in ids])


def _state(plan, **over):
    s = {
        "plan": plan,
        "revision_feedback": "按钮没反应",
        "merged_diff": "",
        "task_description": "做个页面",
        "subtask_results": {},
    }
    s.update(over)
    return s


def _llm_payload(sid: str) -> str:
    return json.dumps({"revision_subtasks": [{
        "id": sid, "description": "修复按钮", "difficulty": "medium",
        "scope": {"writable": ["a.py"]},
    }]}, ensure_ascii=False)


def test_second_revise_same_llm_id_renamed_to_first_free_rev_n():
    """B-1 主场景：plan=[st-1, rev-1(第一轮修订已完成)]，LLM 第二轮仍按示例返 "rev-1"
    → 必须确定性改写为 rev-2，且派发清单/并行组同步拿新 id。"""
    plan = _plan("st-1", "rev-1")
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_llm_payload("rev-1"))):
        out = asyncio.run(nodes.revision(_state(plan)))
    new_ids = [st.id for st in out["plan"].subtasks]
    assert new_ids == ["st-1", "rev-1", "rev-2"], "撞名必须改写为第一个空闲 rev-N"
    assert len(new_ids) == len(set(new_ids)), "plan 内 id 必须唯一（撞名绝不再入 plan）"
    assert out["dispatch_remaining"] == ["rev-2"], "派发清单必须拿改写后的新 id"
    assert out["plan"].parallel_groups[-1] == ["rev-2"], "并行组必须拿改写后的新 id"


def test_collision_rename_emits_warning_with_both_ids(caplog):
    """降级留痕铁律：撞名改写必须一次 WARNING 且同条带旧/新两个 id（机读可辨）。"""
    plan = _plan("st-1", "rev-1")
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_llm_payload("rev-1"))), \
            caplog.at_level(logging.WARNING):
        asyncio.run(nodes.revision(_state(plan)))
    hits = [r for r in caplog.records
            if r.levelno >= logging.WARNING and "撞名" in r.getMessage()]
    assert hits, "撞名改写必须 WARNING 留痕"
    msg = hits[0].getMessage()
    assert "rev-1" in msg and "rev-2" in msg, "WARNING 必须同条带旧 id 与新 id"


def test_no_collision_keeps_llm_id_and_stays_silent(caplog):
    """反向锁：不撞名时 LLM 给的 id 原样保留、零撞名 WARNING（闸不得误伤正常路径）。"""
    plan = _plan("st-1")
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_llm_payload("rev-1"))), \
            caplog.at_level(logging.WARNING):
        out = asyncio.run(nodes.revision(_state(plan)))
    assert out["plan"].subtasks[-1].id == "rev-1"
    assert out["dispatch_remaining"] == ["rev-1"]
    assert not [r for r in caplog.records if "撞名" in r.getMessage()]


def test_first_free_rev_n_skips_occupied():
    """「第一个空闲」判据：rev-1/rev-3 已占、rev-2 空闲 → 撞名 rev-3 必须改写 rev-2。
    若误实现为 max+1 会得 rev-4——本锁区分两种实现。"""
    plan = _plan("st-1", "rev-1", "rev-3")
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_llm_payload("rev-3"))):
        out = asyncio.run(nodes.revision(_state(plan)))
    assert out["plan"].subtasks[-1].id == "rev-2"


def test_default_id_fallback_path_also_passes_gate():
    """三条构造路径同一咽喉：JSON 解析失败走默认 id `rev-{len+1}`，撞名照样改写。
    夹具：plan=[st-1, rev-3]，len=2 → 默认 id=rev-3 撞名 → 改写第一个空闲 rev-1。"""
    plan = _plan("st-1", "rev-3")
    with patch.object(nodes, "_get_brain_llm", _fake_llm("这不是 JSON")):
        out = asyncio.run(nodes.revision(_state(plan)))
    assert out["plan"].subtasks[-1].id == "rev-1", \
        "兜底路径的默认 id 撞名同样必须改写（三条路径同一咽喉）"
    assert out["dispatch_remaining"] == ["rev-1"]
    # 双复核 R1 hunter LOW#2：兜底路径同样断 parallel_groups——闸若被挪到 append 之后，
    # 旧 id 会静默滞留并行组（subtasks/dispatch 断言逮不到这一层）。
    assert out["plan"].parallel_groups[-1] == ["rev-1"]


def test_stale_result_key_not_in_plan_still_renamed():
    """双复核 R1 hunter MEDIUM 直达锁：判重源必须含 subtask_results——`_is_ready` 的
    完成判定权威源是 completed_l1_ids(subtask_results)，而「结果账键 ⊆ plan ids」目前
    靠 replan 外科过滤/拆小 pop 逐路径维持（无强闸）。构造【不在 plan 里却在结果账】的
    陈旧 rev-1：LLM 返 rev-1 照样必须改写（否则该形态绕过 plan-only 判重，B-1 换皮复发）。
    ★R2 LOW②：夹具刻意 l1_passed=False——闸挡的是【任何结果账键】（.keys() 直读），
    与 L1 是否通过无关；若误读成「只有通过的产出才算占用」而把判重源换成
    completed_l1_ids()，滞留失败键会被放行 → 旧失败结果被同名新任务静默覆盖★。"""
    plan = _plan("st-1")
    stale = {"rev-1": WorkerOutput(subtask_id="rev-1", diff="+x", summary="旧产出",
                                   l1_passed=False)}
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_llm_payload("rev-1"))):
        out = asyncio.run(nodes.revision(_state(plan, subtask_results=stale)))
    assert out["plan"].subtasks[-1].id == "rev-2", \
        "撞结果账陈旧键（不在 plan）同样必须改写——判重源=plan∪subtask_results∪failed"
    assert out["dispatch_remaining"] == ["rev-2"]
    assert "rev-1" in out["subtask_results"], "旧完成态产出必须保留（只改名不丢账）"


def test_failed_subtask_id_not_in_plan_still_renamed():
    """双复核 R2 hunter LOW①：failed_subtask_ids 判重源独立锁——failed id 既不在 plan
    也不在结果账（如拆小路径 pop 后的残留形态）时，撞名照样改写。删掉
    `| set(state.get("failed_subtask_ids") or [])` 行本锁必须红（否则新 revision 复用
    旧失败 id，继承其 redecompose/dispatch_totals 等按 id 键的旧账=配额污染）。"""
    plan = _plan("st-1")
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_llm_payload("rev-1"))):
        out = asyncio.run(nodes.revision(
            _state(plan, subtask_results={}, failed_subtask_ids=["rev-1"])))
    assert out["plan"].subtasks[-1].id == "rev-2", \
        "撞 failed_subtask_ids 占用键（不在 plan/结果账）同样必须改写"
    assert out["dispatch_remaining"] == ["rev-2"]


def test_plan_absent_but_results_present_still_gated():
    """plan_obj=None 不再整段跳闸（hunter MEDIUM ②）：结果账非空时撞名照样改写。"""
    stale = {"rev-1": WorkerOutput(subtask_id="rev-1", diff="+x", summary="旧产出",
                                   l1_passed=True)}
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_llm_payload("rev-1"))):
        out = asyncio.run(nodes.revision(_state(None, subtask_results=stale)))
    assert out["plan"].subtasks[-1].id == "rev-2"
    assert out["dispatch_remaining"] == ["rev-2"]


def test_renamed_revision_subtask_dispatchable_end_to_end():
    """接线锁（finding 因果链全程）：改写后的 rev-2 带着 completed={st-1,rev-1}
    进真 `TaskPlan.get_dispatch_batch` → 批次非空且就是 rev-2（修订真的会被派发）。"""
    plan = _plan("st-1", "rev-1")
    with patch.object(nodes, "_get_brain_llm", _fake_llm(_llm_payload("rev-1"))):
        out = asyncio.run(nodes.revision(_state(plan)))
    batch = out["plan"].get_dispatch_batch(
        completed_ids={"st-1", "rev-1"},
        dispatch_remaining=out["dispatch_remaining"],
        max_concurrent=4,
    )
    assert [t.id for t in batch] == ["rev-2"], \
        "改写后的修订子任务必须真可就绪派发（原病=恒空批→熔断 MERGE 吞修订）"


def test_duplicate_id_starves_dispatch_batch_characterization():
    """原病定性锚（锁住「为何必须有闸」的因果事实）：plan 内 id 撞名 + 已完成同 id
    → get_dispatch_batch 恒空批。本锁测的是 _is_ready 语义本身，与闸无关恒绿——
    ★它是病因档案，不是 revision 闸的回归证据（删闸本锁不红，红的是上面 5 条）★。"""
    dup_plan = TaskPlan(
        subtasks=[_st("st-1"), _st("rev-1"), _st("rev-1")],
        parallel_groups=[["st-1"], ["rev-1"], ["rev-1"]],
    )
    batch = dup_plan.get_dispatch_batch(
        completed_ids={"st-1", "rev-1"},
        dispatch_remaining=["rev-1"],
        max_concurrent=4,
    )
    assert batch == [], "撞名 id 恒判不就绪=空批（B-1 原病形态，闸存在的理由）"
