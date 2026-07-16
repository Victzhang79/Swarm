"""R38-F（round38 治本 #6）：REVIEW_DESIGN 对账 file_plan 完整性。

round38 实测：TECH_DESIGN 报 7/9 模块设计失败（ERROR"勿当成功"）后 2ms，
review_design 自动模式径直"方案通过"——审查对失败清单零对账，残缺 file_plan
（58 文件=2/9 模块）静默流向 PLAN。

治本：
  - review_design 自动模式：tech_design_failed_modules 非空 → 打回（带 repair_only
    标记 + 缺失清单反馈），受既有 design_rejects 上限约束（到顶带 degraded 强制继续，
    不死循环）；人工模式不变（interrupt 面板已可见 degraded）。
  - tech_design 打回重入：见 repair_only → 外科补齐——只对缺失模块重跑 STAGE2
    （preset_stage1 跳过顶层方案重生成），新 file_plan 与已成模块合并，
    绝不全量重拆（P1 外科补丁先例，round37 R35-C 教训）。
    人工 reject 无 repair_only → 保留全量重做语义。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from swarm.models import ledger


@pytest.fixture(autouse=True)
def _clean_ledger(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    yield
    ledger._reset_for_tests()


def _auto_state(**kw) -> dict:
    st = {"task_id": "t-rf", "session_metadata": {"auto_accept": True}}
    st.update(kw)
    return st


def _run_review(state):
    from swarm.brain.planning_nodes import review_design
    return asyncio.run(review_design(state))


# ─────────────────── review_design 对账 ───────────────────

def test_auto_review_rejects_when_modules_failed(monkeypatch):
    from swarm.brain import planning_nodes as pn
    monkeypatch.setattr(pn, "_auto_mode", lambda s: True)
    out = _run_review(_auto_state(
        tech_design_failed_modules=[{"name": "alarm-web", "idx": 8, "reason": "x"}]))
    dr = out["design_review"]
    assert dr["decision"] == "reject"
    assert dr.get("repair_only") is True
    assert "alarm-web" in dr["feedback"]
    assert dr["reject_count"] == 1


def test_auto_review_approves_when_no_failures(monkeypatch):
    from swarm.brain import planning_nodes as pn
    monkeypatch.setattr(pn, "_auto_mode", lambda s: True)
    out = _run_review(_auto_state())
    assert out["design_review"]["decision"] == "approve"


def test_auto_review_forced_continue_at_reject_limit(monkeypatch):
    """补齐 N 次仍缺失 → 带 degraded 强制继续（防死循环），不无限打回。"""
    from swarm.brain import planning_nodes as pn
    monkeypatch.setattr(pn, "_auto_mode", lambda s: True)
    limit = pn._tier_limits()["design_rejects"]
    out = _run_review(_auto_state(
        tech_design_failed_modules=[{"name": "m", "idx": 1, "reason": "x"}],
        design_review={"decision": "reject", "reject_count": limit},
    ))
    assert out["design_review"]["decision"] == "approve"


# ─────────────────── tech_design 外科补齐 ───────────────────

class _FakeResp:
    def __init__(self, content):
        self.content = content


def _prior_td(n=9, done=(2, 3)) -> dict:
    mods = [{"name": f"mod-{i}", "responsibility": "r", "est_files": 1}
            for i in range(1, n + 1)]
    fp = [{"path": f"src/mod-{i}/a.x", "action": "create", "description": "d",
           "module": f"mod-{i}"} for i in done]
    return {"architecture": "arch", "data_model": "dm", "modules": mods,
            "file_plan": fp, "stack": {}}


def test_tech_design_repair_only_regenerates_missing_modules(monkeypatch):
    """外科补齐：只为缺失模块发 LLM 调用（不重跑 STAGE1/已成模块），合并 file_plan。"""
    from swarm.brain import planning_nodes as pn
    from swarm.types import Complexity

    calls: list[str] = []

    class _LLM:
        async def ainvoke(self, messages):
            user = messages[-1]["content"]
            calls.append(user)
            assert "当前要产出 file_plan 的模块" in user, "外科补齐不应重跑 STAGE1"
            for i in range(1, 10):
                if f"模块名：mod-{i}" in user:
                    return _FakeResp(json.dumps({"file_plan": [
                        {"path": f"src/mod-{i}/b.x", "action": "create",
                         "description": "d"}]}))
            raise AssertionError(f"不认识的模块 prompt: {user[:80]}")

    monkeypatch.setattr(pn, "_get_brain_llm", lambda: _LLM())
    monkeypatch.setattr(pn, "_gather_project_facts", lambda p: "facts")
    monkeypatch.setattr(pn, "_verify_named_files_exist", lambda d, p: [])
    monkeypatch.setattr(pn, "_resolve_project_path", lambda s: None)

    failed = [{"name": f"mod-{i}", "idx": i, "reason": "token limit exceeded"}
              for i in (1, 4, 5, 6, 7, 8, 9)]
    prior = _prior_td()
    state = {
        "task_id": "t-repair",
        "assessed_complexity": Complexity.ULTRA,
        "task_description": "task",
        "tech_design": prior,
        "tech_design_file_plan": list(prior["file_plan"]),
        "tech_design_fact_issues": [],
        "shared_contract_draft": {"keep": True},
        "tech_design_failed_modules": failed,
        "design_review": {"decision": "reject", "reject_count": 1,
                          "repair_only": True,
                          "feedback": "只补齐缺失模块"},
    }
    out = asyncio.run(pn.tech_design(state))
    # R65-T1 起 stage2 为分批续写协议：每模块 1 产出批 + 1 空批确认 = 2 次调用；
    # 本断言核心语义不变——只有 7 个【缺失】模块被重生成，已成模块 0 调用
    assert len(calls) == 14
    assert all(f"模块名：mod-{i}" not in c for c in calls for i in (2, 3)), \
        "已成模块 mod-2/mod-3 不得被重拆"
    fp = out["tech_design_file_plan"]
    paths = {f["path"] for f in fp}
    assert "src/mod-2/a.x" in paths and "src/mod-3/a.x" in paths  # 已成模块保留
    assert len(fp) == 2 + 7
    assert out["tech_design_failed_modules"] == []  # 补齐后清账
    assert out["shared_contract_draft"] == {"keep": True}  # 契约草案不动


def test_tech_design_human_reject_without_marker_full_redo(monkeypatch):
    """人工 reject（无 repair_only）→ 保留全量重做语义：STAGE1 被重跑。"""
    from swarm.brain import planning_nodes as pn
    from swarm.types import Complexity

    calls: list[str] = []

    class _LLM:
        async def ainvoke(self, messages):
            calls.append(messages[-1]["content"])
            return _FakeResp(json.dumps({
                "modules": [], "architecture": "a", "data_model": "d",
                "file_plan": [{"path": "src/x.x", "action": "create",
                               "description": "d"}]}))

    monkeypatch.setattr(pn, "_get_brain_llm", lambda: _LLM())
    monkeypatch.setattr(pn, "_gather_project_facts", lambda p: "facts")
    monkeypatch.setattr(pn, "_verify_named_files_exist", lambda d, p: [])
    monkeypatch.setattr(pn, "_resolve_project_path", lambda s: None)

    state = {
        "task_id": "t-full",
        "assessed_complexity": Complexity.ULTRA,
        "task_description": "task",
        "tech_design": _prior_td(),
        "tech_design_failed_modules": [{"name": "mod-1", "idx": 1, "reason": "x"}],
        "design_review": {"decision": "reject", "reject_count": 1,
                          "feedback": "架构方向不对，重做"},
    }
    asyncio.run(pn.tech_design(state))
    assert calls, "应发起 LLM 调用"
    assert "当前要产出 file_plan 的模块" not in calls[0]  # 首调用是 STAGE1 全量重做
