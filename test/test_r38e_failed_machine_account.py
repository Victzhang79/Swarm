"""R38-E（round38 治本 #5）：FAILED 终态也带机读账。

round38 实测：任务被 token 护栏打成 FAILED 后，API task.error=None、token_usage={}、
degraded_summary=null——audit 日志里有 error 串但任务记录没有，违背 runbook §6
"收尾第一步先读机读账"的设计（机读账只在 DONE/PARTIAL(deliver) 路径接线）。

治本：
  - task_records 增 error TEXT 列（幂等迁移，先例 auto_accept/base_commit）；
    update_task 支持 error=；_TASK_SELECT/行映射透出。
  - runner 资源护栏 FAILED 两分支（无产物 / checkpoint 不可读）落 error 串 +
    token_usage（ledger 权威快照 + salvage_reason + degraded_summary）。
"""

from __future__ import annotations

import pytest

from swarm.models import ledger


@pytest.fixture(autouse=True)
def _clean_ledger(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    yield
    ledger._reset_for_tests()


def test_update_task_supports_error_field():
    """store.update_task 接受 error=（列已迁移）。签名级断言（不落真库）。"""
    import inspect

    from swarm.project import store
    sig = inspect.signature(store.update_task)
    assert "error" in sig.parameters, "update_task 缺 error 参数"


def test_task_select_includes_error_column():
    from swarm.project import store
    assert "error" in store._TASK_SELECT, "_TASK_SELECT 未透出 error 列"


def test_task_select_light_includes_error_column():
    """复核 F5：任务列表轻查询（运维主观察面）同样透出 error——FAILED 无原因还得点详情。"""
    from swarm.project import store
    assert "error" in store._TASK_SELECT_LIGHT


def test_failed_terminal_writes_machine_account(monkeypatch):
    """无产物 FAILED：update_task 必须带 error 串 + token_usage（ledger 快照+salvage_reason
    +degraded_summary），不再裸写 status。"""
    import asyncio

    from swarm.brain import runner as rn

    # ledger 里造一笔真账（权威快照来源）
    ledger.attach("t-fe", budget_total=500_000)
    ledger.set_stage("t-fe", "plan")
    rid = ledger.reserve("t-fe", est_in=20_000, est_out=5_000)
    ledger.settle(rid, real_in=20_000, real_out=7_000)

    calls: list[dict] = []

    def _fake_update_task(task_id, **kw):
        calls.append({"task_id": task_id, **kw})
        return {"id": task_id}

    monkeypatch.setattr(rn.store, "update_task", _fake_update_task)
    monkeypatch.setattr(rn.store, "get_task", lambda tid: {"id": tid, "project_id": "p"})
    monkeypatch.setattr(rn, "_sync_task_from_state", lambda *a, **k: None)
    monkeypatch.setattr(rn, "_emit_task_notification", lambda *a, **k: None)
    monkeypatch.setattr(rn, "_count_completed_in_plan", lambda state: 0)

    class _Q:
        async def publish(self, *a, **k):
            pass

    state = {"task_id": "t-fe", "degraded_reasons": [
        "requirements_extract:empty(llm_failed:x)",
        "requirements_extract:source_truncated"]}
    status = asyncio.run(rn._finalize_governor_partial(
        "t-fe", state, _Q(), reason_code="token_budget_exceeded",
        reason_msg="撞云端 token 预算护栏"))

    assert status == "FAILED"
    failed_writes = [c for c in calls if c.get("status") == "FAILED"]
    assert failed_writes, "未写 FAILED 状态"
    fw = failed_writes[0]
    assert fw.get("error"), "FAILED 终态未落 error 串"
    assert "token_budget_exceeded" in fw["error"]
    tu = fw.get("token_usage") or {}
    assert tu.get("salvage_reason") == "token_budget_exceeded"
    assert tu.get("cloud_tokens_in") == 20_000 and tu.get("cloud_tokens_out") == 7_000
    assert tu.get("degraded_summary"), "FAILED 终态未带 degraded_summary"


def test_checkpoint_unreadable_failed_also_writes_error(monkeypatch):
    """checkpoint 不可读的 FAILED 兜底分支同样落 error（不留裸 FAILED）。"""
    import asyncio

    from swarm.brain import runner as rn

    calls: list[dict] = []
    monkeypatch.setattr(
        rn.store, "update_task",
        lambda task_id, **kw: calls.append({"task_id": task_id, **kw}) or {"id": task_id})
    monkeypatch.setattr(rn.store, "get_task", lambda tid: {"id": tid, "project_id": "p"})
    monkeypatch.setattr(rn, "_emit_task_notification", lambda *a, **k: None)
    monkeypatch.setattr(rn, "_stop_watchdog", lambda tid: None)

    async def _none(tid):
        return None

    monkeypatch.setattr(rn, "_load_state_snapshot", _none)

    class _Q:
        async def publish(self, *a, **k):
            pass

    asyncio.run(rn._salvage_partial_from_checkpoint(
        "t-ck", _Q(), reason_code="token_budget_exceeded", reason_msg="护栏"))
    fw = [c for c in calls if c.get("status") == "FAILED"]
    assert fw and fw[0].get("error"), "checkpoint 兜底 FAILED 未落 error"
