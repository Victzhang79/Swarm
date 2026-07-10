"""R38-C（round38 治本 #2）：TaskTokenLimitExceeded 拒后恢复——等在飞结算再准入。

round38 实测：STAGE2 9 模块×3 重试在 33ms 内全部打完（零退避）；CONTRACT_MODULE 退避
2s/4s，但在飞调用 settle 要 103-408s——两者都结构性等不到预留释放，"暂时性预留紧张"
被固化为模块永久丢失（settle 后余量实际充足）。

治本：
  - ledger.reserve 拒绝时 usage 带 requested_est（等待方判断"释放后是否够"的依据）。
  - ledger.admission_probe(task_id, est)："fit"（现在就够）/"wait"（在飞释放后够）/
    "hopeless"（全释放也不够——等待无意义，立即确定性放弃）。
  - planning_nodes._await_token_admission：轮询 probe，fit→True；hopeless/超时→False；
    在【信号量外】等待（不占并发槽）。
  - 三个规划重试循环（TECH_DESIGN-STAGE2 / CONTRACT_SKELETON / CONTRACT_MODULE）接线：
    token-limit 拒绝 → 等准入再重试；hopeless/超时 → 立即放弃不空转。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from swarm.models import ledger
from swarm.models.errors import TaskTokenLimitExceeded


@pytest.fixture(autouse=True)
def _clean_ledger(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    yield
    ledger._reset_for_tests()


# ─────────────────── reserve 拒绝带 requested_est ───────────────────

def test_reserve_denial_carries_requested_est_total_gate():
    ledger.attach("c1", budget_total=500)
    with pytest.raises(TaskTokenLimitExceeded) as ei:
        ledger.reserve("c1", est_in=400, est_out=200, kind="cloud")
    assert ei.value.usage.get("requested_est") == 600


def test_reserve_denial_carries_requested_est_stage_gate():
    ledger.attach("c2", budget_total=1000)
    ledger.set_stage("c2", "plan")  # plan 子预算 = 250
    with pytest.raises(TaskTokenLimitExceeded) as ei:
        ledger.reserve("c2", est_in=200, est_out=100, kind="cloud")
    u = ei.value.usage
    assert u.get("stage") == "plan" and u.get("requested_est") == 300


def test_ensure_budget_denial_carries_requested_est():
    """复核 F4：ensure_budget 与 reserve 对齐带 requested_est——缺它准入等待会无信息放行。"""
    ledger.attach("c2b", budget_total=1000)
    with pytest.raises(TaskTokenLimitExceeded) as ei:
        ledger.ensure_budget("c2b", min_tokens=5000)
    assert ei.value.usage.get("requested_est") == 5000
    ledger.set_stage("c2b", "plan")  # 子预算 250
    with pytest.raises(TaskTokenLimitExceeded) as ei2:
        ledger.ensure_budget("c2b", min_tokens=300)
    assert ei2.value.usage.get("requested_est") == 300


# ─────────────────── admission_probe 三态 ───────────────────

def test_admission_probe_three_states():
    ledger.attach("c3", budget_total=1000)
    ledger.set_stage("c3", "plan")  # 子预算 250
    # fit：无占用，est 100 ≤ 250
    assert ledger.admission_probe("c3", 100) == "fit"
    # wait：在飞预留 200 占满，est 100 现在不进（200+100>250）但释放后进（0+100≤250）
    rid = ledger.reserve("c3", est_in=150, est_out=50, kind="cloud")
    assert ledger.admission_probe("c3", 100) == "wait"
    # 释放后 fit
    ledger.settle(rid, real_in=10, real_out=10)  # spent=20
    assert ledger.admission_probe("c3", 100) == "fit"
    # hopeless：est 300 > 250-20 —— 全释放也不够
    assert ledger.admission_probe("c3", 300) == "hopeless"


def test_admission_probe_trackonly_always_fit():
    ledger.attach("c4", budget_total=0)
    assert ledger.admission_probe("c4", 10**9) == "fit"


# ─────────────────── _await_token_admission ───────────────────

def test_await_admission_waits_for_settle():
    """在飞 settle 释放后准入返回 True（round38 正解：等 103-408s 的 settle 而非 33ms 烧完）。"""
    from swarm.brain import planning_nodes as pn

    ledger.attach("c5", budget_total=1000)
    ledger.set_stage("c5", "plan")
    rid = ledger.reserve("c5", est_in=150, est_out=50, kind="cloud")  # 在飞占满

    async def _run():
        async def _settle_later():
            await asyncio.sleep(0.15)
            ledger.settle(rid, real_in=10, real_out=10)
        t = asyncio.ensure_future(_settle_later())
        ok = await pn._await_token_admission(
            "c5", {"requested_est": 100}, max_wait_s=5.0, poll_s=0.05)
        await t
        return ok

    assert asyncio.run(_run()) is True


def test_await_admission_hopeless_returns_false_fast():
    from swarm.brain import planning_nodes as pn

    ledger.attach("c6", budget_total=1000)
    ledger.set_stage("c6", "plan")  # 子预算 250

    async def _run():
        import time as _t
        t0 = _t.monotonic()
        ok = await pn._await_token_admission(
            "c6", {"requested_est": 400}, max_wait_s=30.0, poll_s=0.05)
        return ok, _t.monotonic() - t0

    ok, elapsed = asyncio.run(_run())
    assert ok is False
    assert elapsed < 1.0  # hopeless 立即放弃，不空等 max_wait_s


def test_await_admission_timeout_returns_false():
    from swarm.brain import planning_nodes as pn

    ledger.attach("c7", budget_total=1000)
    ledger.set_stage("c7", "plan")
    ledger.reserve("c7", est_in=150, est_out=50, kind="cloud")  # 在飞永不结算

    async def _run():
        return await pn._await_token_admission(
            "c7", {"requested_est": 100}, max_wait_s=0.2, poll_s=0.05)

    assert asyncio.run(_run()) is False


def test_await_admission_missing_est_falls_back_permissive():
    """异常被包装丢失 usage 时不阻塞既有重试路径（宁可退回旧行为不误杀）。"""
    from swarm.brain import planning_nodes as pn

    async def _run():
        return await pn._await_token_admission("c8", {}, max_wait_s=1.0, poll_s=0.05)

    assert asyncio.run(_run()) is True


# ─────────────────── STAGE2 循环接线 ───────────────────

class _FakeResp:
    def __init__(self, content: str):
        self.content = content


def _stage1_content(n_modules: int = 1) -> str:
    return json.dumps({
        "modules": [{"name": f"mod-{i}", "responsibility": "r", "est_files": 1}
                    for i in range(n_modules)],
        "architecture": "arch", "data_model": "dm",
        "fact_issues": [], "shared_contract": {},
    })


def test_stage2_recovers_after_admission(monkeypatch):
    """STAGE2 首次撞 token 闸 → 等到准入 → 重试成功，模块不丢。"""
    from swarm.brain import planning_nodes as pn

    probes = iter(["wait", "fit"])
    monkeypatch.setattr(
        "swarm.models.ledger.admission_probe",
        lambda task_id, est, kind="cloud": next(probes))
    monkeypatch.setattr("swarm.models.ledger.widen_budget", lambda *a, **k: None)
    cfg = type("Cfg", (), {"max_task_tokens": 0, "max_task_tokens_per_module": 0})()
    monkeypatch.setattr(pn, "get_config", lambda: cfg)

    calls = {"n": 0}

    class _LLM:
        async def ainvoke(self, messages):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResp(_stage1_content(1))
            if calls["n"] == 2:
                raise TaskTokenLimitExceeded({"requested_est": 100, "total": 1})
            return _FakeResp(json.dumps({"file_plan": [
                {"path": "src/a.x", "action": "create", "description": "d"}]}))

    state = {"task_id": "t-adm", "clarify_summary": ""}
    result, file_plan, *_ = asyncio.run(pn._tech_design_staged(
        _LLM(), "task", "ultra", True, state, "facts", "", ""))
    assert len(file_plan) == 1
    assert not result.get("stage2_failed_modules")


def test_stage2_hopeless_gives_up_without_pointless_retries(monkeypatch):
    """hopeless（全释放也不够）→ 立即确定性放弃：LLM 不再被空转重试 3 次。"""
    from swarm.brain import planning_nodes as pn

    monkeypatch.setattr(
        "swarm.models.ledger.admission_probe",
        lambda task_id, est, kind="cloud": "hopeless")
    monkeypatch.setattr("swarm.models.ledger.widen_budget", lambda *a, **k: None)
    cfg = type("Cfg", (), {"max_task_tokens": 0, "max_task_tokens_per_module": 0})()
    monkeypatch.setattr(pn, "get_config", lambda: cfg)

    stage2_calls = {"n": 0}

    class _LLM:
        async def ainvoke(self, messages):
            if stage2_calls.get("stage1_done"):
                stage2_calls["n"] += 1
                raise TaskTokenLimitExceeded({"requested_est": 10**9, "total": 1})
            stage2_calls["stage1_done"] = True
            return _FakeResp(_stage1_content(1))

    state = {"task_id": "t-hope", "clarify_summary": ""}
    result, file_plan, *_ = asyncio.run(pn._tech_design_staged(
        _LLM(), "task", "ultra", True, state, "facts", "", ""))
    assert stage2_calls["n"] == 1  # 只发起一次，绝不 3 连空转
    assert result.get("stage2_failed_modules")
