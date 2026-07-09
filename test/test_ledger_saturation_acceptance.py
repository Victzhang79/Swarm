"""阶段1.6（§九 末尾验收判据）：模拟饱和 provider——空转烧钱在代码上不再是合法路径。

§九 原文：注入一个模拟饱和 provider（100% 超时），任务必须在 ledger 阶段预算内
确定性 escalate 为 PARTIAL/人工，且账本读数 ≥ 真实计费的 90%。

测法（生产 choke 路径逐层）：饱和 provider 每次调用都"吐部分 output chunk 后 stall
被杀"，input 全额计费（服务端已处理 prompt）。驱动无界重试循环（模拟 91min 空转的
规划循环形态），断言：
  ① 循环被 TaskTokenLimitExceeded 确定性终止（非靠轮次计数/墙钟）；
  ② 终止时账本已结算读数 ≥ 饱和 provider 实际计费的 90%（宁可高估的结算口径）；
  ③ 花费被钉死在该阶段子预算内（超出量 ≤ 单次调用预留，即"最后一笔在飞"的天然余量）。
"""

from __future__ import annotations

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


class _Chunk:
    def __init__(self, i, o):
        self.usage_metadata = {"input_tokens": i, "output_tokens": o}


class _SaturatedProvider:
    """100% 超时的云端 provider：处理 prompt（input 计费）、吐 500 token 后 stall 被杀。"""

    def __init__(self):
        self.billed_in = 0
        self.billed_out = 0
        self.calls = 0

    def run_one_call(self, guard: _LedgerGuard, prompt: str):
        rid = uuid.uuid4()
        # 发起（此处预算闸可拒绝——拒绝即一分钱不烧）
        guard.on_llm_start({}, [prompt], run_id=rid)
        # 服务端已处理 prompt：input 全额计费；吐 500 token 后 stall
        self.calls += 1
        est_in = len(prompt) // 3 + 64  # 与 guard 预留口径一致的"真实" input 计费
        self.billed_in += est_in
        self.billed_out += 500
        guard.on_llm_new_token("t", chunk=_Chunk(est_in, 500), run_id=rid)
        guard.on_llm_error(RuntimeError("stream stall timeout —— 基建瞬时"), run_id=rid)


def test_saturated_provider_deterministic_escalate_within_stage_budget():
    task_id = "sat-1"
    budget = 200_000
    ledger.attach(task_id, budget_total=budget)
    ledger.set_stage(task_id, "plan")  # 25% → 50_000
    usage_tracker.set_current_task(task_id)
    stage_limit = int(budget * ledger.DEFAULT_STAGE_RATIOS["plan"])

    provider = _SaturatedProvider()
    guard = _LedgerGuard("cloud", "m", max_tokens=0)
    prompt = "需求" * 3000  # est_in ≈ 2064/次

    escalated = False
    for _ in range(10_000):  # 模拟无界重试循环（唯一合法出口=ledger 闸）
        try:
            provider.run_one_call(guard, prompt)
        except TaskTokenLimitExceeded as exc:
            escalated = True
            assert exc.usage.get("stage") == "plan", "必须带阶段归因（该阶段 escalate）"
            break
    assert escalated, "饱和 provider 下无界循环必须被 ledger 确定性终止——空转烧钱不再合法"

    snap = ledger.snapshot(task_id)
    settled = snap["cloud_tokens_in"] + snap["cloud_tokens_out"]
    billed = provider.billed_in + provider.billed_out
    # ② 账本读数 ≥ 真实计费 90%
    assert settled >= 0.9 * billed, f"账本 {settled} < 90% × 计费 {billed}"
    # ③ 花费钉死在阶段子预算内（余量 ≤ 单次调用预留：最后一笔在飞的天然上界）
    per_call_reserve = len(prompt) // 3 + 64 + guard._DEFAULT_OUT_RESERVE
    assert settled <= stage_limit + per_call_reserve, (
        f"阶段花费 {settled} 超出子预算 {stage_limit} 逾单次预留 {per_call_reserve}")
    # 拒绝发起的调用一分钱不烧：provider 实际被调次数 = 账本 llm_calls
    assert provider.calls == snap["llm_calls"]


def test_saturated_provider_total_budget_cap_without_stage():
    """无阶段（stage=None）时总预算同样确定性兜底。"""
    task_id = "sat-2"
    ledger.attach(task_id, budget_total=30_000)
    usage_tracker.set_current_task(task_id)
    provider = _SaturatedProvider()
    guard = _LedgerGuard("cloud", "m", max_tokens=0)
    with pytest.raises(TaskTokenLimitExceeded):
        for _ in range(10_000):
            provider.run_one_call(guard, "x" * 6000)
    snap = ledger.snapshot(task_id)
    assert snap["cloud_tokens_in"] + snap["cloud_tokens_out"] <= 30_000 + (6000 // 3 + 64 + 4096)
