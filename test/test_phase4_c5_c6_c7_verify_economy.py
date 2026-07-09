"""阶段4 C5+C6+C7（登记册 §四）：verify agent 步经济化 + prompt 矛盾统一 + fix 轮修改记忆。

C5：verify agent 步（每 fix_round 一整轮带工具 agent）在 det 闸门已有结论时近零价值
（llm_ok 恒被仲裁器强制 True），其拒答/截断反而误杀好产出 → 确定性闸门先行，verify
步只在 det_ok=None 时跑；refusal_hard_fail 收窄 sticky=False+可翻盘（拒答是验证通道
artifact，编码产出经 det 闸门另行裁决——provider 一次截断不再永久判死好产出）。
C6：「完成编码后必须实际运行命令」vs 编码阶段「禁止运行重型构建」双 bind 矛盾 →
统一为验证归系统确定性 L1 闸门。
C7：fix 轮每次是全新单条 human 消息（无对话记忆）→ 确定性拼进已改文件清单+关键
新增行，不再重复勘察/把同一 typo 反复写回。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor
from swarm.worker.l1_verdict import L1Verdict, evaluate_l1

_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"


def _mk() -> WorkerExecutor:
    st = SubTask(id="st-c5", description="改 a.py",
                 difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                 scope=FileScope(writable=["a.py"]), intent="modify")
    return WorkerExecutor(subtask=st, project_path="/tmp/swarm-c5-test")


# ─────────────── C5：det 有结论 → verify agent 步不跑 ───────────────

def test_verify_agent_skipped_when_det_decisive(monkeypatch):
    monkeypatch.delenv("SWARM_WORKER_VERIFY_AGENT_STEP", raising=False)
    ex = _mk()
    calls: list[str] = []

    async def fake_agent(prompt, step=""):
        calls.append(step)
        return "PASS"

    with patch.object(ex, "_run_agent", side_effect=fake_agent), \
         patch.object(ex, "_deterministic_l1_gate", return_value=(True, {})), \
         patch.object(ex, "_check_timeout", return_value=False):
        l1_passed, _, verdict = asyncio.run(ex._phase_verify_loop())
    assert l1_passed is True
    assert not any(s.startswith("verify") for s in calls), (
        "det 闸门已有结论（True/False）时 verify agent 步纯烧钱——仲裁器本就强制 llm_ok=True")


def test_verify_agent_runs_when_det_none(monkeypatch):
    monkeypatch.delenv("SWARM_WORKER_VERIFY_AGENT_STEP", raising=False)
    ex = _mk()
    calls: list[str] = []

    async def fake_agent(prompt, step=""):
        calls.append(step)
        return '{"passed": true}'

    with patch.object(ex, "_run_agent", side_effect=fake_agent), \
         patch.object(ex, "_deterministic_l1_gate",
                      return_value=(None, {"not_run_kind": "benign"})), \
         patch.object(ex, "_check_timeout", return_value=False):
        asyncio.run(ex._phase_verify_loop())
    assert any(s.startswith("verify") for s in calls), (
        "det_ok=None（无确定性证据）时仍需 LLM 弱自报兜底——verify 步保留")


# ─────────────── C5：refusal 收窄为可翻盘 ───────────────

def test_refusal_without_det_evidence_is_flippable():
    v = evaluate_l1(det_ok=None, det_details={}, verify_result="",
                    llm_ok=False, prior=None, phase="phase3_loop")
    assert v.passed is False and v.source == "refusal_hard_fail"
    assert v.sticky is False, (
        "verify 步拒答/截断是验证通道 artifact（provider 截断/沙箱限制），"
        "sticky=True 会让一次截断永久判死好产出")


def test_refusal_prior_flips_on_det_evidence():
    prior = L1Verdict(passed=False, source="refusal_hard_fail",
                      reason="截断", sticky=False, details={})
    v = evaluate_l1(det_ok=True, det_details={}, verify_result=None,
                    llm_ok=True, prior=prior, phase="phase4_final")
    assert v.passed is True, (
        "Phase-4 确定性+LLM 双证据到位必须能翻盘 verify 步拒答（好产出不陪葬）")


# ─────────────── C6：prompt 矛盾统一 ───────────────

def test_harness_section_no_longer_orders_running_builds():
    from swarm.worker.prompts import _format_harness_section

    class _H:
        build_command = "mvn -q compile"
        test_command = "mvn -q test"
        verify_commands = []

    txt = _format_harness_section(_H())
    assert "必须实际运行上述命令" not in txt, (
        "与编码阶段『禁止运行重型构建』双 bind 自相矛盾——小模型无所适从")
    assert "确定性 L1 闸门" in txt, "统一口径：验证归系统闸门"


# ─────────────── C7：fix prompt 带修改记忆 ───────────────

def test_fix_prompt_includes_changed_files_memory():
    ex = _mk()
    with patch.object(ex, "_get_git_diff", return_value=_DIFF):
        prompt = ex._build_fix_prompt("verify text", {"error": "x"})
    assert "已修改的文件" in prompt and "a.py" in prompt, (
        "fix 轮无对话记忆——不拼已改文件清单，模型重复勘察/把同一 typo 反复写回")


def test_fix_prompt_degrades_without_diff():
    ex = _mk()
    with patch.object(ex, "_get_git_diff", side_effect=RuntimeError("no git")):
        prompt = ex._build_fix_prompt("verify text", {"error": "x"})
    assert "verify text" in prompt, "记忆块失败必须降级为空，绝不拖垮修复轮"
