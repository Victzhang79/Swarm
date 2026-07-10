"""阶段1.1/1.2（§九 TaskLedger）：单一权威账本核心语义 — 行为测试。

DEEP_READ_REGISTER_2026-07-09_E2E.md §九 A) 记账侧：
  - 单一权威账本 task_id → {cloud_in/out, local, llm_calls, replan_rounds, stage_spent}，
    写穿 DB、resume/重启延续（治 F1：账本进程内存态 resume 归零）。
  - 预留-结算：调用前按估算预留，余额不足拒绝发起（抛 TaskTokenLimitExceeded）；
    完成按真实 usage 结算；error/中止按已收 chunk 估算结算【宁可高估】（治 B4）。
  - 阶段子预算按比例派生（extract5/plan25/execute55/verify15），阶段烧穿=该阶段
    escalate 带 stage 归因，不吃兄弟阶段的份（治 B7 规划循环无聚合预算）。
  - 本地 token 独立列独立闸值（默认 0=不闸，治"只计云端"盲区且不复现 round28 误杀）。
"""

from __future__ import annotations

import pytest

from swarm.models import ledger
from swarm.models.errors import TaskTokenLimitExceeded


@pytest.fixture(autouse=True)
def _clean_ledger(monkeypatch):
    # 每例隔离内存态；断掉真实 DB（单测不落库，落库通道单独用假 pool 测）
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    yield
    ledger._reset_for_tests()


# ─────────────────── 预留-结算 ───────────────────

def test_reserve_settle_basic():
    ledger.attach("t1", budget_total=1000)
    rid = ledger.reserve("t1", est_in=300, est_out=100, kind="cloud")
    snap = ledger.snapshot("t1")
    assert snap["reserved"] == 400
    ledger.settle(rid, real_in=100, real_out=50)
    snap = ledger.snapshot("t1")
    assert snap["reserved"] == 0
    assert snap["cloud_tokens_in"] == 100 and snap["cloud_tokens_out"] == 50
    assert snap["llm_calls"] == 1
    assert ledger.remaining("t1") == 1000 - 150


def test_reserve_rejects_when_budget_insufficient():
    ledger.attach("t2", budget_total=500)
    rid = ledger.reserve("t2", est_in=200, est_out=100, kind="cloud")
    ledger.settle(rid, real_in=200, real_out=100)  # 已花 300
    with pytest.raises(TaskTokenLimitExceeded) as ei:
        ledger.reserve("t2", est_in=300, est_out=100, kind="cloud")  # 300+400 > 500
    u = ei.value.usage
    assert u.get("task_id") == "t2" and u.get("limit_effective") == 500


def test_inflight_reservations_count_against_budget():
    """两笔在飞预留合计超额 → 第二笔拒绝发起（不是等结算才发现）。"""
    ledger.attach("t3", budget_total=500)
    ledger.reserve("t3", est_in=200, est_out=100, kind="cloud")
    with pytest.raises(TaskTokenLimitExceeded):
        ledger.reserve("t3", est_in=200, est_out=100, kind="cloud")


def test_settle_error_overestimates_input():
    """error/中止路径：input 按预留全额（服务端已计费），output 按已收 chunk——宁可高估。"""
    ledger.attach("t4", budget_total=10_000)
    rid = ledger.reserve("t4", est_in=800, est_out=500, kind="cloud")
    ledger.settle_error(rid, chunk_in=0, chunk_out=120)
    snap = ledger.snapshot("t4")
    assert snap["reserved"] == 0
    assert snap["cloud_tokens_in"] == 800, "中止调用 input 必须按预留全额入账（宁可高估）"
    assert snap["cloud_tokens_out"] == 120
    assert snap["llm_calls"] == 1


def test_settle_error_prefers_chunk_max_when_higher():
    """已收 chunk 报的 usage 比估算还大（累计型网关）→ 取大者。"""
    ledger.attach("t5", budget_total=10_000)
    rid = ledger.reserve("t5", est_in=100, est_out=100, kind="cloud")
    ledger.settle_error(rid, chunk_in=900, chunk_out=300)
    snap = ledger.snapshot("t5")
    assert snap["cloud_tokens_in"] == 900 and snap["cloud_tokens_out"] == 300


def test_budget_zero_tracks_but_never_gates():
    ledger.attach("t6", budget_total=0)
    for _ in range(5):
        rid = ledger.reserve("t6", est_in=10_000_000, est_out=0, kind="cloud")
        ledger.settle(rid, real_in=10_000_000, real_out=0)
    assert ledger.snapshot("t6")["cloud_tokens_in"] == 50_000_000


def test_unattached_task_is_track_only():
    """未 attach（预处理期/无任务上下文）→ 自动 track-only，绝不闸。"""
    rid = ledger.reserve("t-ghost", est_in=999_999_999, est_out=0, kind="cloud")
    ledger.settle(rid, real_in=5, real_out=5)
    assert ledger.snapshot("t-ghost")["cloud_tokens_in"] == 5


# ─────────────────── 本地独立列/独立闸值 ───────────────────

def test_local_tokens_separate_column_no_cloud_consumption():
    ledger.attach("t7", budget_total=100)
    rid = ledger.reserve("t7", est_in=5_000, est_out=5_000, kind="local")  # 不撞云端闸
    ledger.settle(rid, real_in=5_000, real_out=5_000)
    snap = ledger.snapshot("t7")
    assert snap["local_tokens"] == 10_000
    assert snap["cloud_tokens_in"] == 0
    assert ledger.remaining("t7") == 100  # 云端余额不受本地影响


def test_local_budget_gates_when_configured():
    ledger.attach("t8", budget_total=0, local_budget=1000)
    rid = ledger.reserve("t8", est_in=400, est_out=400, kind="local")
    ledger.settle(rid, real_in=400, real_out=400)
    with pytest.raises(TaskTokenLimitExceeded):
        ledger.reserve("t8", est_in=300, est_out=0, kind="local")


# ─────────────────── 阶段子预算 ───────────────────

def test_stage_budget_derives_from_ratio_and_gates():
    # R38b-1：钉 ratio（默认已 0.35）；借位顶格 1.5×250=375，超顶格才拒
    ledger.attach("t9", budget_total=1000, stage_ratios={"plan": 0.25})
    ledger.set_stage("t9", "plan")
    with pytest.raises(TaskTokenLimitExceeded) as ei:
        ledger.reserve("t9", est_in=400, est_out=200, kind="cloud")  # 600 > 375
    assert ei.value.usage.get("stage") == "plan", "阶段烧穿必须带 stage 归因"


def test_stage_exhaustion_does_not_eat_sibling_stage():
    # R38b-1：钉 ratio；借位顶格 375——需超顶格才拒（240+200=440 > 375）
    ledger.attach("t10", budget_total=1000, stage_ratios={"plan": 0.25})
    ledger.set_stage("t10", "plan")
    rid = ledger.reserve("t10", est_in=100, est_out=100, kind="cloud")
    ledger.settle(rid, real_in=240, real_out=0)  # plan 几乎烧穿(240/250)
    with pytest.raises(TaskTokenLimitExceeded):
        ledger.reserve("t10", est_in=200, est_out=0, kind="cloud")
    ledger.set_stage("t10", "execute")  # execute=55% → 550，兄弟份不受 plan 影响
    rid2 = ledger.reserve("t10", est_in=300, est_out=100, kind="cloud")
    ledger.settle(rid2, real_in=300, real_out=100)
    assert ledger.snapshot("t10")["stage_spent"]["execute"] == 400


def test_stage_none_only_total_gate():
    ledger.attach("t11", budget_total=1000)
    ledger.set_stage("t11", None)
    rid = ledger.reserve("t11", est_in=800, est_out=100, kind="cloud")  # 超任何单阶段份额但 <总
    ledger.settle(rid, real_in=800, real_out=100)
    assert ledger.remaining("t11") == 100


def test_elastic_budget_update_widens():
    ledger.attach("t12", budget_total=500)
    ledger.set_budget("t12", 2000)  # 规划揭示子任务数 → 弹性放宽
    rid = ledger.reserve("t12", est_in=1000, est_out=0, kind="cloud")
    ledger.settle(rid, real_in=1000, real_out=0)
    assert ledger.remaining("t12") == 1000


# ─────────────────── 重试层接口 / replan 记账 ───────────────────

def test_ensure_budget_for_retry_layers():
    ledger.attach("t13", budget_total=1000)
    rid = ledger.reserve("t13", est_in=900, est_out=0, kind="cloud")
    ledger.settle(rid, real_in=900, real_out=0)
    ledger.ensure_budget("t13", min_tokens=50)  # 剩 100 ≥ 50，放行
    with pytest.raises(TaskTokenLimitExceeded):
        ledger.ensure_budget("t13", min_tokens=200)


def test_note_replan_counts():
    ledger.attach("t14", budget_total=0)
    ledger.note_replan("t14")
    ledger.note_replan("t14")
    assert ledger.snapshot("t14")["replan_rounds"] == 2


# ─────────────────── 落库写穿 + resume 延续（治 F1）───────────────────

def test_resume_restores_settled_from_db(monkeypatch):
    """重启/resume：attach 从 DB 恢复已结算额度——账本不再随进程归零。"""
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: {
        "cloud_tokens_in": 300, "cloud_tokens_out": 100, "local_tokens": 50,
        "llm_calls": 7, "replan_rounds": 1, "stage_spent": {"plan": 400},
    } if task_id == "t15" else None)
    monkeypatch.setattr(ledger, "_flush_row", lambda *a, **k: True)
    ledger.attach("t15", budget_total=1000, stage_ratios={"plan": 0.25})
    snap = ledger.snapshot("t15")
    assert snap["cloud_tokens_in"] == 300 and snap["llm_calls"] == 7
    assert ledger.remaining("t15") == 1000 - 400
    ledger.set_stage("t15", "plan")  # plan 已花 400 > 借位顶格 375 → 立即闸住
    with pytest.raises(TaskTokenLimitExceeded):
        ledger.reserve("t15", est_in=10, est_out=0, kind="cloud")


def test_flush_writes_through(monkeypatch):
    ledger._reset_for_tests()
    monkeypatch.setattr(ledger, "_load_row", lambda task_id: None)
    written: list = []
    monkeypatch.setattr(ledger, "_flush_row",
                        lambda task_id, row: (written.append((task_id, dict(row))), True)[1])
    ledger.attach("t16", budget_total=1000)
    rid = ledger.reserve("t16", est_in=100, est_out=50, kind="cloud")
    ledger.settle(rid, real_in=100, real_out=50)
    ledger.flush()
    assert written and written[-1][0] == "t16"
    assert written[-1][1]["cloud_tokens_in"] == 100


# ─────────────────── 异常迁移 ───────────────────

def test_exception_importable_from_errors_and_runner():
    from swarm.brain.runner import TaskTokenLimitExceeded as FromRunner
    from swarm.models.errors import TaskTokenLimitExceeded as FromErrors
    assert FromRunner is FromErrors, "迁移后必须同一类（runner re-export 保兼容）"
    exc = FromErrors({"total": 42})
    assert exc.usage["total"] == 42 and "42" in str(exc)
