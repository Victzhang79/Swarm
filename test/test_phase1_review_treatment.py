"""阶段1 对抗双复核治理批（2026-07-09）— reviewer(F1 CRITICAL) + hunter(5 CONFIRMED) 全治。

F1 [CRITICAL reviewer·实测坐实]：被取消的 ainvoke() 不触发 on_llm_error（langchain-core
   1.4.0）→ 预留泄漏挂到 detach，虚高预留把预算充裕任务误拒成 PARTIAL。
   修：①预留 TTL 惰性过期（reserve/ensure_budget 时超 TTL 的在飞预留按 settle_error
   语义结算+释放）；②_DualTimeoutChatOpenAI._astream 取消分支主动补发 on_llm_error。
H1 [CONFIRMED hunter]：detach 后迟到结算再造幽灵条目 → 全量 upsert 覆盖 DB 真值。
   修：①attached 标记——只有 attach 过的条目可落库，幽灵条目 flush 周期清理；
   ②gather_cancel_on_error——预算异常逃逸时取消兄弟批/兄弟 worker。
H2 [CONFIRMED]：dispatch 路径把 TaskTokenLimitExceeded 吞成普通子任务失败。修：穿透。
H3 [CONFIRMED]：salvage PARTIAL 第二次 update_task 覆盖丢 limit_exceeded 标记。
   修：merge 继承 + salvage_reason 归因。
H4 [CONFIRMED]：CJK //3 低估 + 无 usage 静默估算。修：共用 CJK 感知估算器 + 每任务一次告警。
H5 [CONFIRMED]：_targeted_coverage_topup 吞预算异常回退全量重拆。修：穿透。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from swarm.models import ledger, usage_tracker
from swarm.models.errors import TaskTokenLimitExceeded
from swarm.models.router import _LedgerGuard


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    usage_tracker.set_current_task(None)
    yield
    usage_tracker.set_current_task(None)
    ledger._reset_for_tests()


# ─────────────────── F1：预留 TTL 惰性过期 ───────────────────

def test_stale_reservation_expires_and_settles(monkeypatch):
    """泄漏预留（取消的 ainvoke 无回调）超 TTL → 下次 reserve 时自动按 input 全额结算释放。"""
    monkeypatch.setenv("SWARM_LEDGER_RESERVATION_TTL_S", "0.01")
    ledger.attach("f1", budget_total=100_000)
    rid = ledger.reserve("f1", est_in=5000, est_out=1000, kind="cloud")
    assert ledger.snapshot("f1")["reserved"] == 6000
    import time
    time.sleep(0.05)
    ledger.reserve("f1", est_in=10, est_out=0, kind="cloud")  # 触发惰性过期
    snap = ledger.snapshot("f1")
    assert snap["cloud_tokens_in"] == 5000, "泄漏预留必须按 input 估算全额结算（宁可高估）"
    assert snap["reserved"] == 10, "泄漏预留必须被释放（否则虚高误拒到任务结束）"
    _ = rid


def test_stale_reservation_expiry_via_ensure_budget(monkeypatch):
    monkeypatch.setenv("SWARM_LEDGER_RESERVATION_TTL_S", "0.01")
    ledger.attach("f1b", budget_total=10_000)
    ledger.reserve("f1b", est_in=9000, est_out=500, kind="cloud")
    import time
    time.sleep(0.05)
    # 过期结算后：spent=9000、reserved=0 → 余 1000 ≥ 512 放行（泄漏若不清则 reserved 卡死一切）
    ledger.ensure_budget("f1b", min_tokens=512)


# ─────────────────── H1a：幽灵条目不落库 ───────────────────

def test_late_settle_after_detach_never_flushes_ghost(monkeypatch):
    written: list = []
    monkeypatch.setattr(ledger, "_flush_row",
                        lambda tid, row: (written.append((tid, dict(row))), True)[1])
    ledger.attach("g1", budget_total=1000)
    rid = ledger.reserve("g1", est_in=100, est_out=0, kind="cloud")
    ledger.settle(rid, real_in=100, real_out=0)
    ledger.detach("g1")  # 真值落库（cloud_in=100）
    _true_rows = [r for t, r in written if t == "g1"]
    assert _true_rows and _true_rows[-1]["cloud_tokens_in"] == 100
    written.clear()
    # detach 后迟到的 worker 结算（幽灵）
    rid2 = ledger.reserve("g1", est_in=50, est_out=0, kind="cloud")
    ledger.settle(rid2, real_in=7, real_out=0)
    ledger.flush()
    assert all(t != "g1" for t, _ in written), (
        "幽灵条目落库=全量 upsert 把 DB 真值覆盖成近空行，毁掉 resume 延续")
    ledger.flush()  # 幽灵条目被周期清理（无在飞预留）
    assert ledger.snapshot("g1") == {}


# ─────────────────── H1b：gather 兄弟取消 ───────────────────

async def test_gather_cancel_on_error_cancels_siblings():
    from swarm.brain.nodes.shared import gather_cancel_on_error
    cancelled: list = []

    async def _slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
        return "done"

    async def _boom():
        await asyncio.sleep(0.01)
        raise TaskTokenLimitExceeded({"total": 1})

    with pytest.raises(TaskTokenLimitExceeded):
        await gather_cancel_on_error([_slow(), _boom(), _slow()])
    assert len(cancelled) == 2, "兄弟任务必须被取消（否则各跑满超时继续烧钱+幽灵结算）"


async def test_gather_cancel_on_error_success_passthrough():
    from swarm.brain.nodes.shared import gather_cancel_on_error

    async def _ok(v):
        return v

    assert await gather_cancel_on_error([_ok(1), _ok(2)]) == [1, 2]


# ─────────────────── H2：dispatch 路径穿透 ───────────────────

async def test_dispatch_to_worker_reraises_token_limit(monkeypatch):
    import swarm.brain.nodes as nodes
    from swarm.types import FileScope, SubTask, SubTaskDifficulty

    class _BoomDispatcher:
        async def dispatch(self, *a, **k):
            raise TaskTokenLimitExceeded({"total": 1})

    # _dispatch_to_worker 内部 get_worker_dispatcher() 的 seam
    import swarm.infra.worker_dispatcher as wd
    monkeypatch.setattr(wd, "get_worker_dispatcher", lambda: _BoomDispatcher())
    st = SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a"]))
    with pytest.raises(TaskTokenLimitExceeded):
        await nodes._dispatch_to_worker(st, {}, project_id="p", task_id="t")


# ─────────────────── H4：CJK 估算器 ───────────────────

def test_cjk_aware_estimator():
    cjk = "需" * 170  # ≈100 token（1.7 char/tok）
    ascii_ = "x" * 350  # ≈100 token（3.5 char/tok）
    assert 90 <= ledger.estimate_tokens_text(cjk) <= 110
    assert 90 <= ledger.estimate_tokens_text(ascii_) <= 110
    # 原 //3 口径对 CJK：170//3=56 ——低估近半
    assert ledger.estimate_tokens_text(cjk) > len(cjk) // 3


def test_guard_uses_cjk_estimator():
    ledger.attach("h4", budget_total=1_000_000)
    usage_tracker.set_current_task("h4")
    guard = _LedgerGuard("cloud", "m", max_tokens=100)
    guard.on_llm_start({}, ["需" * 1700], run_id=uuid.uuid4())
    reserved = ledger.snapshot("h4")["reserved"]
    assert reserved >= 1000 + 100, f"CJK prompt 预留必须按 ~1.7 char/tok（got {reserved}）"


# ─────────────────── H5：topup 穿透 ───────────────────

async def test_coverage_topup_reraises_token_limit():
    from swarm.brain.nodes import _targeted_coverage_topup
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    class _BoomLLM:
        async def ainvoke(self, messages):
            raise TaskTokenLimitExceeded({"total": 1})

    prior = TaskPlan(
        subtasks=[SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=["a"]), covers=["req-aaaa1111"])],
        parallel_groups=[["st-1"]])
    with pytest.raises(TaskTokenLimitExceeded):
        await _targeted_coverage_topup(
            _BoomLLM(), prior, [{"id": "req-bbbb2222", "text": "t"}],
            {"req-aaaa1111", "req-bbbb2222"}, fallback_llm=None)
