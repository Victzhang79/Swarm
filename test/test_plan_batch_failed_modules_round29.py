"""round29 真因4：PLAN-BATCH 单模块分解失败=静默丢整模块（伪装成功）治本。

现场（task d37a52a3）：批 3/7 'system-enhance'（14 文件）两次 timeout → FINDING-10 降级跳过 →
「6/7 模块成功，合并出 31 个子任务（失败 1）」——失败模块【不落任何 state 键】（对比
TECH_DESIGN 有 stage2_failed_modules 记账），任务若其余部分成功会记 DONE 但交付物静默缺
整模块 + LEARN_SUCCESS 学成成功模式；规则5 已两告「12 artifacts 落空」（跨模块契约悬空）。

治本（镜像 tech_design_failed_modules W1.1 先例）：
1. `_plan_ultra_batched` 返回 (TaskPlan, plan_batch_failed_modules)——失败模块结构化记账
   （name/files/reason），timeout 可经 SWARM_PLAN_BATCH_TIMEOUT 调节（原硬码 300s 不可测）。
2. plan 节点 always-emit `plan_batch_failed_modules`（BrainState 声明防 LangGraph 静默丢键；
   last-write-wins 使 replan 成功后自动清空不粘滞）；非空时并入 degraded_reasons
   （should_write_success 据此拦 L6 假成功学习）。
3. `gates.can_auto_accept_plan`：plan_batch_failed_modules 非空 → fail-fast 拒 auto_accept
   升人工（与 tech_design_incomplete 同格）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


_OK_SUBTASKS = {
    "subtasks": [
        {
            "id": "st-1",
            "description": "实现 alarm-sdk 基础能力",
            "scope": {"create_files": ["alarm-sdk/src/A.java"], "writable": [], "readable": []},
        }
    ]
}


class _FakeLLM:
    """按 prompt 内容决定行为：含 hang_module 的批 → 睡过超时；其余 → 返回合法子任务。"""

    def __init__(self, hang_module: str | None = None, empty_module: str | None = None):
        self.hang_module = hang_module
        self.empty_module = empty_module

    async def ainvoke(self, msgs):
        user = msgs[-1]["content"]

        class R:
            content = json.dumps(_OK_SUBTASKS, ensure_ascii=False)

        if self.hang_module and f"'{self.hang_module}'" in user:
            await asyncio.sleep(0.8)  # 超过测试注入的 0.2s 墙钟 → wait_for 取消
        if self.empty_module and f"'{self.empty_module}'" in user:
            R.content = json.dumps({"subtasks": []})
        return R()


def _state():
    return {
        "tech_design": {"modules": [
            {"name": "alarm-sdk", "depends_on": []},
            {"name": "system-enhance", "depends_on": []},
        ]},
        "shared_contract_draft": {},
        "project_id": "",
    }


_FILE_PLAN = [
    {"path": "alarm-sdk/src/A.java", "module": "alarm-sdk", "action": "create"},
    {"path": "system-enhance/src/B.java", "module": "system-enhance", "action": "create"},
    {"path": "system-enhance/src/C.java", "module": "system-enhance", "action": "create"},
]


def _run_batched(llm, monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "0.2")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    from swarm.brain.nodes import _plan_ultra_batched

    return asyncio.run(_plan_ultra_batched(
        llm, _state(), "需求描述", {}, "", list(_FILE_PLAN),
    ))


# ═════════ 1. 结构化记账：timeout 模块必须进 failed 列表（修前红：返回值不是二元组）═════════
def test_batched_planner_records_timeout_module(monkeypatch):
    plan, failed = _run_batched(_FakeLLM(hang_module="system-enhance"), monkeypatch)
    assert [m["name"] for m in failed] == ["system-enhance"], failed
    assert failed[0]["reason"] == "timeout"
    assert failed[0]["files"] == 2, "必须记录丢失的文件数（14 文件蒸发这类量级要可见）"
    # 幸存模块照常拆出（降级容错语义不回归）
    assert plan.subtasks and any("alarm-sdk" in (f or "")
                                 for st in plan.subtasks
                                 for f in (st.scope.create_files or []))


def test_batched_planner_all_ok_returns_empty_failed(monkeypatch):
    plan, failed = _run_batched(_FakeLLM(), monkeypatch)
    assert failed == []
    assert plan.subtasks


def test_batched_planner_records_empty_module(monkeypatch):
    """重试耗尽仍拆出 0 子任务的模块 = 同样静默丢，必须记账（reason=empty）。"""
    _plan, failed = _run_batched(_FakeLLM(empty_module="system-enhance"), monkeypatch)
    assert [m["name"] for m in failed] == ["system-enhance"]
    assert failed[0]["reason"] == "empty"


# ═════════ 复核 A：非法 SWARM_PLAN_BATCH_TIMEOUT 按配置错误归因，不裸穿成"LLM 失败" ═════════
def test_invalid_timeout_env_falls_back_with_config_attribution(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "abc")  # 非法值
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    from swarm.brain.nodes import _plan_ultra_batched

    # 不抛 ValueError（回退默认 300s），全模块正常拆出
    plan, failed = asyncio.run(_plan_ultra_batched(
        _FakeLLM(), _state(), "需求描述", {}, "", list(_FILE_PLAN),
    ))
    assert failed == []
    assert plan.subtasks


# ═════════ 复核 B：个别子任务字段畸形 → 按模块记账剔除，不连坐丢全部 ═════════
def test_invalid_subtask_recorded_not_cascading(monkeypatch):
    class _BadFieldLLM(_FakeLLM):
        async def ainvoke(self, msgs):
            user = msgs[-1]["content"]

            class R:
                content = json.dumps(_OK_SUBTASKS, ensure_ascii=False)

            if "'system-enhance'" in user:  # 该模块吐畸形 difficulty 枚举
                R.content = json.dumps({"subtasks": [{
                    "id": "st-9", "description": "bad",
                    "difficulty": "not-a-real-enum",
                    "scope": {"create_files": ["system-enhance/src/B.java"],
                              "writable": [], "readable": []},
                }]})
            return R()

    plan, failed = _run_batched(_BadFieldLLM(), monkeypatch)
    # 幸存模块产出保留（不连坐）
    assert plan.subtasks and any("alarm-sdk" in (f or "")
                                 for st in plan.subtasks
                                 for f in (st.scope.create_files or []))
    # 畸形子任务按模块记账（reason 带真实校验错误）
    assert any(m["name"] == "system-enhance" and m["reason"].startswith("invalid_subtasks")
               for m in failed), failed


# ═════════ 2. CONFIRM 闸门：非空 fail-fast 拒 auto_accept（镜像 W1.1）═════════
def test_gate_rejects_plan_batch_failed_modules():
    from swarm.brain.gates import can_auto_accept_plan

    ok_state = {"plan_valid": True, "plan_batch_failed_modules": []}
    allow, _ = can_auto_accept_plan(ok_state)
    assert allow is True

    bad_state = {
        "plan_valid": True,
        "plan_batch_failed_modules": [{"name": "system-enhance", "files": 14, "reason": "timeout"}],
    }
    allow, reason = can_auto_accept_plan(bad_state)
    assert allow is False
    assert "system-enhance" in reason, reason
    assert "plan_batch" in reason or "分解失败" in reason, reason


# ═════════ 2b. CONFIRM 归因（复盘补漏）：auto_accept 撞闸门 → fail-fast REJECT +
# verification_failure="plan_batch_failed"（专类归因，不得误标 plan_invalid 污染 L5）═════════
def test_confirm_auto_accept_rejects_with_dedicated_attribution():
    from swarm.brain.nodes import confirm_plan
    from swarm.brain.state import Complexity, HumanDecision

    state = {
        "auto_accept": True,
        "plan_valid": True,
        "complexity": Complexity.ULTRA,
        "plan_batch_failed_modules": [{"name": "system-enhance", "files": 14, "reason": "timeout"}],
    }
    out = confirm_plan(state)
    assert out["human_decision"] == HumanDecision.REJECT, "丢模块计划绝不能 auto-ACCEPT"
    assert out.get("verification_failure") == "plan_batch_failed", (
        f"须专类归因（非 plan_invalid），实际 {out.get('verification_failure')}"
    )
    assert out.get("failure_escalated") is True, "须升级人工（与 tech_design_incomplete 同格）"


# ═════════ 3. BrainState 声明（防 LangGraph 未声明键静默丢）═════════
def test_brainstate_declares_plan_batch_failed_modules():
    from swarm.brain.state import BrainState

    assert "plan_batch_failed_modules" in BrainState.__annotations__, (
        "plan_batch_failed_modules 必须声明进 BrainState——LangGraph 未声明键会被静默丢弃，"
        "闸门读不到=治本失效（CODEWALK 批4a 历史坑）"
    )
