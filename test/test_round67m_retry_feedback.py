"""R67M-T2 B1（23号文，round67m FAILED@PLAN 主死因治本）行为测试：重试反馈修复记忆。

round67m 四轮打回实证：VALIDATE→PLAN 重试只注【上轮】issues+全量重拆=非单调振荡
（轮4 CVB shadow 与轮1 逐字相同=纯回归，烧 3h15m k3 全量重产）。治本=修复记忆：
  ① increment_retry（validate→plan 重试唯一必经边）单点累积历轮 issues 去重账；
  ② PLAN 注入点把"历轮曾现而本轮已消失"的缺陷作"绝不许回归"硬约束注入
     （_no_regress_feedback_block 差集段）；
  ③ 清空纪律：validate 通过 / REVISE·failure replan 新周期（与 prev_structural 同点）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.graph import _increment_plan_retry  # noqa: E402
from swarm.brain.nodes import _no_regress_feedback_block  # noqa: E402


# ─── ① increment_retry 单点累积（修复记忆入账） ───


def test_increment_retry_accumulates_issue_history():
    """历轮 issues 去重累积：轮1 [a,b] → 轮2 [b,c] 得 [a,b,c]（保序去重）。"""
    out1 = _increment_plan_retry({"plan_retry_count": 0,
                                  "plan_validation_issues": ["缺陷A", "缺陷B"]})
    assert out1["plan_retry_count"] == 1
    assert out1["plan_validation_issue_history"] == ["缺陷A", "缺陷B"]
    out2 = _increment_plan_retry({
        "plan_retry_count": out1["plan_retry_count"],
        "plan_validation_issue_history": out1["plan_validation_issue_history"],
        "plan_validation_issues": ["缺陷B", "缺陷C"]})
    assert out2["plan_retry_count"] == 2
    assert out2["plan_validation_issue_history"] == ["缺陷A", "缺陷B", "缺陷C"], \
        "历轮 issues 必须去重累积（round67m 轮4 CVB=轮1 回归的反面）"


def test_increment_retry_blank_issues_filtered_and_no_history_key_safe():
    """空/空白 issue 不入账；state 无历史键（旧 checkpoint）安全起算。"""
    out = _increment_plan_retry({"plan_validation_issues": ["", "  ", "真缺陷"]})
    assert out["plan_validation_issue_history"] == ["真缺陷"]
    assert out["plan_retry_count"] == 1


def test_increment_retry_no_issues_keeps_history():
    """本轮 issues 空（边界）→ 历史账原样保留不清。"""
    out = _increment_plan_retry({"plan_validation_issue_history": ["旧缺陷"],
                                 "plan_validation_issues": []})
    assert out["plan_validation_issue_history"] == ["旧缺陷"]


# ─── ② 反回归反馈段（绝不许回归差集） ───


def test_no_regress_block_lists_fixed_issues_only():
    """差集=历轮账−本轮 issues：已修掉的列出、本轮仍失败的不列（由"务必逐条修正"段承载）。"""
    state = {
        "plan_validation_issue_history": ["CVB shadow 撞 base", "st-1 考卷矛盾", "st-11 考卷矛盾"],
        "plan_validation_issues": ["st-11 考卷矛盾"],
    }
    block = _no_regress_feedback_block(state)
    assert "绝不许回归" in block
    assert "CVB shadow 撞 base" in block and "st-1 考卷矛盾" in block
    assert "st-11 考卷矛盾" not in block, "本轮仍失败的 issue 归修正段，不得在反回归段重复"


def test_no_regress_block_empty_when_no_history_or_all_current():
    """首轮重试（无历史账）/历轮缺陷全部未愈 → 空串零噪声。"""
    assert _no_regress_feedback_block({"plan_validation_issues": ["x"]}) == ""
    assert _no_regress_feedback_block({
        "plan_validation_issue_history": ["x"], "plan_validation_issues": ["x"]}) == ""


def test_no_regress_block_round67m_wheel4_shape():
    """round67m 轮4 形态实证：轮3 修好 CVB（历史账含 CVB）→ 轮4 反馈必带 CVB 反回归段
    （有 B1 则轮4 重产时 CVB 在"绝不许回归"段里，不再被摇回来）。"""
    state = {
        # 轮4 起点：历轮账=轮1 的 CVB×1 + st-1 + 轮2 的 st-11（去重后）；本轮 issues=st-11
        # 残留一条（注入发生在重产前，已修好的 CVB/st-1 必须落入"绝不许回归"差集段）
        "plan_validation_issue_history": ["CVB shadow", "st-1 考卷矛盾", "st-11 考卷矛盾"],
        "plan_validation_issues": ["st-11 考卷矛盾"],
    }
    block = _no_regress_feedback_block(state)
    assert "CVB shadow" in block, "修好的 CVB 必须在反回归段（round67m 轮4 死因的正面证据）"


# ─── ③ 清空纪律（复核 reviewer MEDIUM-1 = hunter LOW-6：漏清=跨周期粘滞，机制另一半）───
# 六个清空点全行为级驱动：confirm REVISE（interrupt 打桩）+ failure.py 三 replan 出口
# （runtime/L2/执行失败）+ validate 通过 2 处（SIMPLE/主路径过重未见便宜驱动面——
# 由 ② 差集测试与 increment 累积测试夹住，validate 通过即清在 test_r64_t3 同基建上仍
# 需全闸绿 plan fixture，登记为 backlog 补強项，此处先把四个 replan/REVISE 出口钉死）。

import asyncio  # noqa: E402
import json as _json  # noqa: E402
from unittest.mock import patch  # noqa: E402

import swarm.brain.nodes as _nodes  # noqa: E402
from swarm.types import (  # noqa: E402
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


def _mini_plan():
    return TaskPlan(subtasks=[
        SubTask(id="st-1", description="脚手架", difficulty=SubTaskDifficulty.MEDIUM,
                modality=SubTaskModality.TEXT, scope=FileScope(create_files=["m/pom.xml"])),
        SubTask(id="st-2", description="切片", difficulty=SubTaskDifficulty.TRIVIAL,
                modality=SubTaskModality.TEXT,
                scope=FileScope(create_files=["m/src/A.java"]), depends_on=["st-1"]),
    ], parallel_groups=[["st-1"]])


class _OfflineLLM:
    def __call__(self):
        raise RuntimeError("brain_offline_llm_blocked: 测试模拟离线")


def test_revise_clears_issue_history(monkeypatch):
    """CONFIRM 人工 REVISE=新规划周期 → 修复记忆清空（nodes/__init__.py REVISE 重置块）。"""
    monkeypatch.setattr(_nodes, "interrupt",
                        lambda _payload: {"decision": "revise", "feedback": "改一下"})
    # confirm_plan 读 os.environ SWARM_AUTO_ACCEPT——.env 残留（全量跑加载）会把测试
    # 推进 auto_accept fail-fast 分支而到不了 interrupt（已知 .env 污染族同型）。
    monkeypatch.delenv("SWARM_AUTO_ACCEPT", raising=False)
    out = _nodes.confirm_plan({
        "plan_valid": False, "complexity": Complexity.MEDIUM,
        "plan_validation_issue_history": ["旧缺陷X"],
    })
    assert out.get("plan_validation_issue_history") == [], \
        "REVISE 新周期必须清修复记忆（否则旧账跨周期粘滞误注'绝不许回归'）"
    assert out.get("plan_retry_count") == 0


def test_runtime_smoke_replan_clears_issue_history():
    """runtime 冒烟失败归因不出 → replan 出口清修复记忆（failure.py:728 同键）。"""
    state = {
        "complexity": Complexity.ULTRA,
        "plan": _mini_plan(),
        "verification_failure": "runtime_smoke",
        "runtime_smoke_details": {"classification": "code_error"},
        "replan_count": 0,
        "failed_subtask_ids": [],
        "subtask_results": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
        "plan_validation_issue_history": ["旧缺陷X"],
    }
    with patch.object(_nodes, "_get_brain_llm", _OfflineLLM()):
        out = asyncio.run(_nodes.handle_failure(state))
    assert out.get("failure_strategy") == "replan", f"应走 replan 出口: {out.get('failure_strategy')}"
    assert out.get("plan_validation_issue_history") == [], \
        "runtime replan=新规划周期，修复记忆必须清空"


def test_l2_replan_clears_issue_history():
    """L2 集成失败（未定向归因）→ replan 出口清修复记忆（failure.py:827 同键）。"""
    state = {
        "complexity": Complexity.ULTRA,
        "plan": _mini_plan(),
        "verification_failure": "l2",
        "l2_passed": False,
        "l2_targeted": False,
        "replan_count": 0,
        "failed_subtask_ids": [],
        "subtask_results": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
        "plan_validation_issue_history": ["旧缺陷X"],
    }
    with patch.object(_nodes, "_get_brain_llm", _OfflineLLM()):
        out = asyncio.run(_nodes.handle_failure(state))
    assert out.get("failure_strategy") == "replan", f"应走 replan 出口: {out.get('failure_strategy')}"
    assert out.get("plan_validation_issue_history") == [], \
        "L2 replan=新规划周期，修复记忆必须清空"


def test_subtask_failure_replan_clears_issue_history():
    """执行期子任务失败 LLM 裁决 replan → 出口清修复记忆（failure.py:1962 同键）。"""
    l1d = {"deterministic_gate": "verify", "det_fail_reason": "verify_failed: grep X"}
    state = {
        "complexity": Complexity.ULTRA,
        "plan": _mini_plan(),
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {
            "st-2": WorkerOutput(subtask_id="st-2", diff="x", summary="verify 未过",
                                 l1_passed=False, l1_details=l1d),
        },
        "subtask_retry_counts": {"st-2": 99},  # 配额耗尽 → 确定性回退也保 replan 方向
        "dispatch_remaining": [],
        "degraded_reasons": [],
        "plan_validation_issue_history": ["旧缺陷X"],
    }
    payload = _json.dumps({"strategy": "replan", "reasoning": "重规划"}, ensure_ascii=False)

    class _R:
        content = payload

    class _L:
        async def ainvoke(self, _m):
            return _R()

    with patch.object(_nodes, "_get_brain_llm", lambda: _L()):
        out = asyncio.run(_nodes.handle_failure(state))
    assert out.get("failure_strategy") == "replan", f"应走 replan 出口: {out.get('failure_strategy')}"
    assert out.get("plan_validation_issue_history") == [], \
        "执行失败 replan=新规划周期，修复记忆必须清空"
