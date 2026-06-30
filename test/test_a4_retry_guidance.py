"""A4 治本(round11)：把 brain 失败诊断作为硬约束注入【重试 worker 提示】。

round11: brain 明写"该 RuoYi 版本用 ShiroUtils 而非 SecurityUtils"却只 retry_alternate 换模型、
不传 worker → 重试 worker 仍 import 不存在的 SecurityUtils 再次编译失败。治本：HANDLE_FAILURE 把
诊断挂到失败子任务的 retry_guidance，build_worker_prompt 渲染为硬约束块，所有 retry 分支携带。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.types import (
    Complexity, FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan, WorkerOutput,
)
from swarm.worker.prompts import build_worker_prompt


# ── ① build_worker_prompt 渲染 retry_guidance ──
def test_prompt_renders_retry_guidance_block():
    st = SubTask(
        id="st-9", description="实现 2FA 服务", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(create_files=["a/Svc.java"]),
        retry_guidance="该 RuoYi 版本使用 ShiroUtils；禁止 import com.ruoyi.common.utils.SecurityUtils",
    )
    p = build_worker_prompt(st)
    assert "上次失败的诊断与硬约束" in p, "应渲染诊断硬约束块"
    assert "ShiroUtils" in p and "禁止 import com.ruoyi.common.utils.SecurityUtils" in p
    # 描述本体仍在
    assert "实现 2FA 服务" in p


def test_prompt_no_block_when_guidance_empty():
    st = SubTask(id="st-1", description="x", scope=FileScope(create_files=["a/A.java"]))
    assert "上次失败的诊断与硬约束" not in build_worker_prompt(st)


# ── ② HANDLE_FAILURE 把诊断挂到失败子任务 ──
def _plan():
    return TaskPlan(subtasks=[
        SubTask(id="st-1", description="脚手架", difficulty=SubTaskDifficulty.MEDIUM,
                modality=SubTaskModality.TEXT, scope=FileScope(create_files=["m/pom.xml"])),
        SubTask(id="st-9", description="2FA", difficulty=SubTaskDifficulty.MEDIUM,
                modality=SubTaskModality.TEXT,
                scope=FileScope(create_files=["m/src/Svc.java"]), depends_on=["st-1"]),
    ], parallel_groups=[["st-1"]])


class _FakeResp:
    def __init__(self, content): self.content = content


def _fake_llm(reasoning: str, strategy="retry"):
    import json as _j
    payload = _j.dumps({"strategy": strategy, "reasoning": reasoning}, ensure_ascii=False)

    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp(payload)
    return lambda: _L()


def test_handle_failure_attaches_diagnosis_to_failed_subtask():
    diag = "该 RuoYi 版本用 ShiroUtils 而非 SecurityUtils；BaseController 方法是 getDataTable() 不是 getDTable()"
    state = {
        "complexity": Complexity.ULTRA,
        "plan": _plan(),
        "failed_subtask_ids": ["st-9"],
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff="d", summary="ok", l1_passed=True),
            "st-9": WorkerOutput(subtask_id="st-9", diff="", summary="编译失败", l1_passed=False,
                                 l1_details={"build_output": "cannot find symbol: class SecurityUtils"}),
        },
        "subtask_retry_counts": {"st-9": 1},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    with patch.object(nodes, "_get_brain_llm", _fake_llm(diag)):
        out = asyncio.run(nodes.handle_failure(state))
    plan = out.get("plan")
    st9 = next(s for s in plan.subtasks if s.id == "st-9")
    assert st9.retry_guidance and "ShiroUtils 而非 SecurityUtils" in st9.retry_guidance, \
        "失败子任务应携带 brain 诊断(A4)，供重试 worker 提示渲染"
    # 成功兄弟不应被挂诊断
    st1 = next(s for s in plan.subtasks if s.id == "st-1")
    assert not st1.retry_guidance, "成功子任务不应被挂重试诊断"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for fn in fns:
        try:
            fn(); print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            bad += 1; print(f"  ❌ {fn.__name__}: {e}")
    sys.exit(1 if bad else 0)
