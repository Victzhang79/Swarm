"""阶段1.3（§九 TaskLedger）：router 单点闸 _LedgerGuard + B4 error 入账 — 行为测试。

  - _LedgerGuard 挂在 get_chat_model 唯一 chokepoint：on_llm_start 按 prompt 长度+
    max_tokens 预留，余额不足抛 TaskTokenLimitExceeded【拒绝发起】（raise_error=True）；
    on_llm_end 真实结算；on_llm_error 按已收 chunk 结算（input 宁可高估）。
  - B4：_UsageRecorder.on_llm_error 不再 pop 丢弃——已收 chunk 的 usage 照常入
    usage_tracker（中止/超时/掐流正是饱和期最贵形态）。
"""

from __future__ import annotations

import uuid

import pytest

from swarm.models import ledger, usage_tracker
from swarm.models.errors import TaskTokenLimitExceeded
from swarm.models.router import _LedgerGuard, _UsageRecorder


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


class _Resp:
    def __init__(self, i, o, text=""):
        self.llm_output = {"token_usage": {"prompt_tokens": i, "completion_tokens": o}}
        self.generations = []


def test_guard_reserves_then_settles_real_usage():
    ledger.attach("g1", budget_total=100_000)
    usage_tracker.set_current_task("g1")
    guard = _LedgerGuard("cloud", "m", max_tokens=1000)
    rid = uuid.uuid4()
    guard.on_llm_start({}, ["x" * 3000], run_id=rid)
    assert ledger.snapshot("g1")["reserved"] >= 1000, "发起时必须预留（prompt//3 + max_tokens）"
    guard.on_llm_end(_Resp(800, 200), run_id=rid)
    snap = ledger.snapshot("g1")
    assert snap["reserved"] == 0
    assert snap["cloud_tokens_in"] == 800 and snap["cloud_tokens_out"] == 200


def test_guard_rejects_call_when_budget_exhausted():
    """余额不足 → on_llm_start 抛 TaskTokenLimitExceeded（拒绝发起，不烧钱）。"""
    ledger.attach("g2", budget_total=500)
    usage_tracker.set_current_task("g2")
    guard = _LedgerGuard("cloud", "m", max_tokens=1000)
    assert guard.raise_error is True, "langchain 默认吞回调异常——必须 raise_error=True 才能中止调用"
    with pytest.raises(TaskTokenLimitExceeded):
        guard.on_llm_start({}, ["x" * 3000], run_id=uuid.uuid4())


def test_guard_error_settles_from_chunks_overestimating_input():
    """流中途被杀：按已收 chunk 结算，input 取 max(chunk, 预留估算) 宁可高估。"""
    ledger.attach("g3", budget_total=1_000_000)
    usage_tracker.set_current_task("g3")
    guard = _LedgerGuard("cloud", "m", max_tokens=0)
    rid = uuid.uuid4()
    guard.on_llm_start({}, ["y" * 9000], run_id=rid)   # est_in = 3064
    guard.on_llm_new_token("t", chunk=_Chunk(2000, 150), run_id=rid)
    guard.on_llm_new_token("t", chunk=_Chunk(2000, 500), run_id=rid)  # 累计型取 max
    guard.on_llm_error(RuntimeError("stream stall killed"), run_id=rid)
    snap = ledger.snapshot("g3")
    assert snap["reserved"] == 0, "error 路径必须释放预留（否则预留泄漏卡死后续调用）"
    assert snap["cloud_tokens_in"] >= 2000 and snap["cloud_tokens_out"] == 500
    assert snap["llm_calls"] == 1


def test_guard_noop_without_task_context():
    """无任务归属（预处理/探测）→ 不预留不闸，调用零影响。"""
    guard = _LedgerGuard("cloud", "m", max_tokens=0)
    guard.on_llm_start({}, ["z" * 100_000], run_id=uuid.uuid4())  # 不抛
    assert guard._rids == {}


def test_guard_local_kind_never_hits_cloud_budget():
    ledger.attach("g4", budget_total=100)  # 云端预算极小
    usage_tracker.set_current_task("g4")
    guard = _LedgerGuard("local", "m", max_tokens=0)
    rid = uuid.uuid4()
    guard.on_llm_start({}, ["x" * 30000], run_id=rid)  # 本地大调用不撞云端闸
    guard.on_llm_end(_Resp(5000, 3000), run_id=rid)
    snap = ledger.snapshot("g4")
    assert snap["local_tokens"] == 8000 and snap["cloud_tokens_in"] == 0


def test_usage_recorder_error_path_records_chunks_b4():
    """B4：_UsageRecorder.on_llm_error 把已收 chunk usage 入 usage_tracker（不再丢弃）。"""
    recorded: list = []
    import swarm.models.usage_tracker as ut
    orig = ut.record

    def _spy(pid, kind, prov, model, prompt_tokens, completion_tokens, duration_ms=0):
        recorded.append((kind, prompt_tokens, completion_tokens))

    ut_record_patch = _spy
    try:
        ut.record = ut_record_patch
        rec = _UsageRecorder("cloud", "prov", "m")
        rid = uuid.uuid4()
        rec.on_llm_start({}, ["x"], run_id=rid)
        rec.on_llm_new_token("t", chunk=_Chunk(1200, 340), run_id=rid)
        rec.on_llm_error(RuntimeError("killed"), run_id=rid)
    finally:
        ut.record = orig
    assert recorded == [("cloud", 1200, 340)], (
        f"中止调用已收 chunk 必须入账（B4），got={recorded}")


def test_get_chat_model_attaches_guard_single_point():
    """单点挂载：get_chat_model 构造的模型回调链含 _LedgerGuard（全路径覆盖的物证）。"""
    from swarm.config.settings import get_config
    from swarm.models.router import EndpointProvider
    cfg = get_config().model
    pc = None
    for p in cfg._effective_providers():
        pc = p
        break
    assert pc is not None, "至少应有一个 provider 配置"
    model = EndpointProvider(pc, cfg).get_chat_model("test-model-ledger", 0.1)
    cbs = model.callbacks or []
    assert any(isinstance(cb, _LedgerGuard) for cb in cbs)
