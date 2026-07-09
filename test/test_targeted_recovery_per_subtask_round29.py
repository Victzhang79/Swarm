"""round29 遗漏项#2：定向恢复配额【任务级全局】→ 挤兑饿死后续受害者（st-25 缺 redis starter 死循环）。

现场（task d37a52a3）：targeted_recovery_count 上限 2 被 23:00(st-4-1)/00:04(st-4-1-1 波)
用光 → st-25 撞同类缺依赖时"已达上限落兜底"，【从未拿到 pom 写权】（00:34-00:50 实录
"由于 pom.xml 不在可写范围内"）→ 迭代上限/900s 空烧 → +24 abandon 波主推手。
坐标链路本就齐全（worker 有 pom 写权即可自行补依赖 + Central metadata 版本对账兜底），
卡死在配额挤兑这一环。round29-A 复核 #7 的共享配额注记同源。

治本：配额改【按子任务计】（targeted_recovery_counts: dict，每个受害者各 _tr_max 次，
同子任务环安全语义不变）；全局 targeted_recovery_count 保留仅作遥测。A2 缺依赖阶梯与
round29-A 序修复阶梯同步切换。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan, WorkerOutput


def _st(sid, *, writable=None, create=None, depends=None):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(writable=writable or [], create_files=create or []),
        harness=TaskHarness(language="java"),
        depends_on=depends or [],
    )


def _wo(sid, l1_passed, details=None):
    return WorkerOutput(
        subtask_id=sid,
        diff="--- a/X\n+++ b/X\n@@ -1 +1,2 @@\n a\n+b\n" if l1_passed else "",
        summary="", l1_passed=l1_passed, l1_details=details or {},
        confidence="high" if l1_passed else "low",
    )


_MISSING_DEP_DETAILS = {
    "build_output": ("[ERROR] /workspace/ruoyi-alarm/src/main/java/S.java:[3,43] "
                     "package org.springframework.data.redis.core does not exist\n"
                     "[ERROR] cannot find symbol\n  symbol:   class StringRedisTemplate"),
}


def _missing_dep_state(counts: dict | None, failed: list[str]):
    """构造缺依赖失败态：失败子任务的 scope 落在 ruoyi-alarm 模块（可推出模块 pom）。"""
    subtasks = [
        _st("st-ok", writable=["ruoyi-alarm/src/main/java/OK.java"]),
        _st("st-a", writable=["ruoyi-alarm/src/main/java/A.java"]),
        _st("st-25", writable=["ruoyi-alarm/src/main/java/S.java"]),
    ]
    results = {"st-ok": _wo("st-ok", True)}
    for fid in failed:
        results[fid] = _wo(fid, False, dict(_MISSING_DEP_DETAILS))
    return {
        "plan": TaskPlan(subtasks=subtasks),
        "failed_subtask_ids": list(failed),
        "subtask_results": results,
        "subtask_retry_counts": {fid: 3 for fid in failed},
        "dispatch_remaining": [],
        "targeted_recovery_counts": dict(counts or {}),
        # 旧全局计数已被别的子任务用光——正是 d37a52a3 挤兑现场
        "targeted_recovery_count": 2,
        "project_id": "",
    }


def _run(state, strategy="retry"):
    from swarm.brain.nodes import handle_failure

    async def _fake_invoke(self, msgs):
        class R:
            content = '{"strategy": "%s", "reasoning": "x"}' % strategy
        return R()

    with patch("swarm.brain.nodes._get_brain_llm") as mock_llm:
        inst = mock_llm.return_value
        inst.ainvoke = _fake_invoke.__get__(inst)
        return asyncio.run(handle_failure(state))


# ═════ 1. 挤兑修复：全局配额耗尽但【本子任务】从未用过 → 仍获定向恢复（修前红）═════
def test_new_victim_gets_recovery_despite_global_exhaustion():
    state = _missing_dep_state(counts={"st-a": 2}, failed=["st-25"])
    result = _run(state)
    assert result.get("targeted_recovery_counts"), (  # 3.8 演进：布尔死键已删，看配额表
        f"st-25 从未用过定向恢复配额，不得被其它子任务的用量饿死；实际 strategy="
        f"{result.get('failure_strategy')}"
    )
    # 授权发生（pom 写权授予路径走通）且按子任务记账
    counts = result.get("targeted_recovery_counts") or {}
    assert counts.get("st-25") == 1, counts
    # 成功兄弟保留、失败者重入派发
    assert "st-ok" in result["subtask_results"]
    assert "st-25" in result["dispatch_remaining"]


# ═════ 2. 同子任务环安全不回归：本子任务自身耗尽 → 落常规不再 mutate ═════
def test_same_subtask_loop_safety_preserved():
    state = _missing_dep_state(counts={"st-25": 2}, failed=["st-25"])
    result = _run(state)
    assert result.get("targeted_recovery") is not True, (
        "st-25 自身已用满配额，必须落常规兜底（防 grant→fail→grant 无限循环）"
    )
    counts = result.get("targeted_recovery_counts") or state["targeted_recovery_counts"]
    assert counts.get("st-25") == 2, "耗尽后不得再自增"


# ═════ 3. 混合批：部分耗尽部分新 → 新受害者照常恢复 ═════
def test_mixed_batch_grants_only_fresh_victims():
    state = _missing_dep_state(counts={"st-a": 2}, failed=["st-a", "st-25"])
    result = _run(state)
    assert result.get("targeted_recovery_counts")  # 3.8 演进：布尔死键已删，看配额表
    counts = result.get("targeted_recovery_counts") or {}
    assert counts.get("st-25") == 1
    assert counts.get("st-a") == 2, "已耗尽者不得再自增"


# ═════ 4. replan 重入修剪（hunter MEDIUM）：签名不一致=语义新子任务，不继承旧配额 ═════
def test_replan_reset_prunes_stale_quota():
    from swarm.brain.nodes import _surgical_replan_reset

    old_plan = TaskPlan(subtasks=[_st("st-1", writable=["a/A.java"])])
    # replan 重编号后 st-1 语义变了（scope 不同）→ 旧配额必须被修剪
    new_plan = TaskPlan(subtasks=[_st("st-1", writable=["b/B.java"])])
    out = _surgical_replan_reset({}, old_plan, new_plan,
                                 old_recovery_counts={"st-1": 2, "st-gone": 1})
    assert out.get("targeted_recovery_counts") == {}, out
    # 签名完全一致 → 配额保留（同子任务环安全跨 replan 仍有效）
    same = TaskPlan(subtasks=[_st("st-1", writable=["a/A.java"])])
    out2 = _surgical_replan_reset({}, old_plan, same,
                                  old_recovery_counts={"st-1": 2})
    assert out2.get("targeted_recovery_counts") == {"st-1": 2}, out2


# ═════ 5. BrainState 声明（LangGraph 未声明键静默丢）═════
def test_brainstate_declares_per_subtask_counts():
    from swarm.brain.state import BrainState

    assert "targeted_recovery_counts" in BrainState.__annotations__
