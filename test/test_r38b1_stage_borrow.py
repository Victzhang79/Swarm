"""R38b-1（round38b 复盘治本，拍板①②③全做）：阶段借位 escalate + 尺寸标定。

round38b（task 58b8fa66）FAILED@PLAN-BATCH：plan 子预算 0.25×2.5M=625k 被上游规划
506k + 三并发大流撞破——**总预算还剩 79% 却被阶段闸杀死**。本轮规划零循环零浪费，
阶段闸拦的是健康任务；其设计初衷（拦规划死循环烧穿）已由覆盖单调合同/replan 水位闸兜底。

① 阶段借位：reserve 阶段拒绝时，若总预算余量充足 → 有界自动借位
   （阶段有效上限 ≤ 1.5×基线），借位入账 stage_borrow（机读账可见）+ WARNING fail-loud；
   超 1.5× 或总预算不足 → 照旧拒绝（真失控仍被拦）。
② admission_probe 镜像借位语义（fit 含可借位；hopeless=连借满 1.5× 也不够）。
③ 尺寸标定：plan ratio 0.25→0.35；per_module 200k→300k（round27 实测规划期 826k、
   round38b 实测 ~750k 双数据点标定）。
"""

from __future__ import annotations

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


_R = {"plan": 0.25}  # 数值敏感测试显式钉比例，不随 ③ 默认值漂移


def test_stage_borrow_when_total_has_headroom():
    """阶段撞顶但总预算余量足 → 自动借位放行（round38b 场景：绝不杀死健康任务）。"""
    ledger.attach("b1", budget_total=10_000, stage_ratios=_R)  # plan 基线 2500
    ledger.set_stage("b1", "plan")
    rid = ledger.reserve("b1", est_in=2_000, est_out=0)
    ledger.settle(rid, real_in=2_000, real_out=0)  # 阶段已花 2000
    # 800 预留：2000+800=2800 > 2500 基线，但 ≤ 1.5×2500=3750 且总余量足 → 借位放行
    ledger.reserve("b1", est_in=800, est_out=0)
    snap = ledger.snapshot("b1")
    assert snap.get("stage_borrow", {}).get("plan", 0) >= 300


def test_stage_borrow_capped_at_1_5x():
    """借位有界：超 1.5×基线仍拒绝——真失控照拦。"""
    ledger.attach("b2", budget_total=10_000, stage_ratios=_R)  # 基线 2500，顶 3750
    ledger.set_stage("b2", "plan")
    with pytest.raises(TaskTokenLimitExceeded):
        ledger.reserve("b2", est_in=3_800, est_out=0)


def test_stage_borrow_denied_without_total_headroom():
    """总预算余量不足时不借位（总闸优先，本来就会拒）。"""
    ledger.attach("b3", budget_total=3_000, stage_ratios=_R)  # 基线 750，顶 1125
    rid = ledger.reserve("b3", est_in=2_500, est_out=0)  # 未设 stage：只走总闸
    ledger.settle(rid, real_in=2_500, real_out=0)  # 总已花 2500
    ledger.set_stage("b3", "plan")
    with pytest.raises(TaskTokenLimitExceeded):
        ledger.reserve("b3", est_in=900, est_out=0)  # 总余量仅 500 < 900，借位无从谈起


def test_admission_probe_mirrors_borrow():
    """probe 语义与 reserve 一致：可借位=fit；连 1.5× 也不够=hopeless。"""
    ledger.attach("b4", budget_total=10_000, stage_ratios=_R)
    ledger.set_stage("b4", "plan")
    rid = ledger.reserve("b4", est_in=2_000, est_out=0)
    ledger.settle(rid, real_in=2_000, real_out=0)
    assert ledger.admission_probe("b4", 800) == "fit"       # 借位内
    assert ledger.admission_probe("b4", 2_000) == "hopeless"  # 2000+2000 > 3750


def test_default_sizes_calibrated():
    """③ 尺寸标定：plan ratio 0.35 / per_module 300k（round27+round38b 双数据点）。"""
    assert ledger.DEFAULT_STAGE_RATIOS["plan"] == 0.35
    from swarm.config.settings import AppConfig
    assert AppConfig().max_task_tokens_per_module == 300_000
