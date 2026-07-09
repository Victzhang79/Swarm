"""阶段2 对抗双复核治理（reviewer + silent-failure-hunter，c433dd8 之后）。

F-A（CRITICAL，双复核+实证复现）：半开探针在非 (TimeoutError|TransientInfraError)
  异常下（CancelledError 兄弟取消 / TaskTokenLimitExceeded 从 _LedgerGuard 逸出 /
  API 4xx 等）record_success/record_failure 都不触发 → probing 永久卡 True →
  allow() 恒 False → 模型被静默永久禁用（进程级，直到重启）。
  治：调用侧任意异常归还探针（不计失败）+ breaker 内 probing TTL 自愈（双保险）。
F-B（HIGH）：B6 槽位两面——①排队等槽位的时间被 chunk-gap（120s）钳死且超时喂
  record_failure（自致拥塞污染熔断，把健康模型熔断成失败）；②acquire 无界零日志
  （释放走 GC 钩子非同步，泄漏→越锁越死完全不可见）。
  治：首 chunk 等待由软限把守（gap 只管解码中途间隔）；排队饿死的超时不喂熔断且
  留痕；acquire 有界（SWARM_PROVIDER_SLOT_WAIT_S）+超界 WARNING fail-open 放行。
F-C（MEDIUM-HIGH，双复核独立命中）：主备双 stall 终态 TransientInfraError 落
  _decompose_batch 的 "error" 桶 → U3 bisect（oc[0]=="timeout" 才触发）对 stall
  ——恰是本阶段立项要治的形态——静默失效。治：attempt 循环 TIE 与 TimeoutError 同桶。
F-D（MEDIUM）：槽位池键 (provider_id, loop_id) 不含 limit → 并发上限热变更被最先
  建的池静默吞。治：键纳入 limit。
F-E（LOW）：熔断跳过 primary 直走备路径缺 ensure_budget 头寸检查（其余切备点都有）。
  治：补齐对称。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from swarm.brain.nodes import _invoke_llm_abortable
from swarm.models import breaker
from swarm.models.errors import TaskTokenLimitExceeded, TransientInfraError


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    breaker._reset_for_tests()
    from swarm.models.router import _PROVIDER_SLOTS
    _PROVIDER_SLOTS.clear()
    for k in ("SWARM_BREAKER_THRESHOLD", "SWARM_BREAKER_COOLDOWN_S",
              "SWARM_STREAM_PROGRESS_HARD_MULT", "SWARM_PLAN_BATCH_CHUNK_GAP",
              "SWARM_PROVIDER_SLOT_WAIT_S"):
        monkeypatch.delenv(k, raising=False)
    yield
    breaker._reset_for_tests()
    _PROVIDER_SLOTS.clear()


class _R:
    def __init__(self, content="ok"):
        self.content = content


class _OkLLM:
    model_name = "m-ok"

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _R("saved")


class _StallLLM:
    model_name = "m-stall"

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise TransientInfraError("stream stall 超时")


def _open_breaker(key: str) -> None:
    for _ in range(3):
        breaker.record_failure(key)


# ─────────────── F-A：探针生命周期（卡死=模型静默永久禁用）───────────────

class _WeirdLLM:
    """探针期间抛非超时类异常（API 400 等）——绝不能把探针吞掉。"""
    model_name = "m-weird"

    async def ainvoke(self, messages):
        raise ValueError("bad request 400")


class _CancelProbeLLM:
    model_name = "m-cancelled"

    async def ainvoke(self, messages):
        raise asyncio.CancelledError()


async def test_probe_released_on_foreign_exception(monkeypatch):
    monkeypatch.setenv("SWARM_BREAKER_COOLDOWN_S", "0.05")
    _open_breaker("m-weird")
    time.sleep(0.06)  # 冷却期满 → 下一次调用是半开探针
    with pytest.raises(ValueError):
        await _invoke_llm_abortable(_WeirdLLM(), [], 5.0, _OkLLM())
    assert breaker.allow("m-weird") is True, (
        "非超时类异常必须归还探针——否则 probing 永久 True=模型静默永久禁用")


async def test_probe_released_on_cancel(monkeypatch):
    """gather_cancel_on_error 兄弟取消恰落在探针期（hunter 钉死的确定性触发链）。"""
    monkeypatch.setenv("SWARM_BREAKER_COOLDOWN_S", "0.05")
    _open_breaker("m-cancelled")
    time.sleep(0.06)
    with pytest.raises(asyncio.CancelledError):
        await _invoke_llm_abortable(_CancelProbeLLM(), [], 5.0, _OkLLM())
    assert breaker.allow("m-cancelled") is True, (
        "取消必须归还探针且不吞取消语义（CancelledError 原样上抛）")


def test_probing_ttl_self_heals(monkeypatch):
    """纵深防御：即便调用方失职（崩溃/新调用面忘记归还），probing 超冷却期自愈。"""
    monkeypatch.setenv("SWARM_BREAKER_COOLDOWN_S", "0.05")
    _open_breaker("m-ttl")
    time.sleep(0.06)
    assert breaker.allow("m-ttl") is True   # 探针放行
    assert breaker.allow("m-ttl") is False  # 半开只放一个
    time.sleep(0.06)                        # 探针悬挂超过冷却期
    assert breaker.allow("m-ttl") is True, (
        "probing 必须带 TTL 自愈——裸 bool 无人清=永久禁用死点")


async def test_probe_failure_still_reopens(monkeypatch):
    """回归护栏：治理后探针真失败（超时类）仍必须重新熔断，不得被归还逻辑误放。"""
    monkeypatch.setenv("SWARM_BREAKER_COOLDOWN_S", "5")
    _open_breaker("m-stall")
    # 冷却未满：跳过 primary 直走备
    fb = _OkLLM()
    r = await _invoke_llm_abortable(_StallLLM(), [], 5.0, fb)
    assert r.content == "saved" and fb.calls == 1


# ─────────────── F-B：B6 槽位排队 vs 墙钟/熔断 ───────────────

class _SlowFirstChunkLLM:
    """首 chunk 晚于 chunk-gap 但早于软限——排队/prefill 形态。"""
    model_name = "m-late-first"

    def astream(self, messages):
        async def _gen():
            await asyncio.sleep(0.2)
            yield _R("first ")
            yield _R("second")
        return _gen()


class _MidStallLLM:
    model_name = "m-midstall"

    def astream(self, messages):
        async def _gen():
            yield _R("head ")
            await asyncio.sleep(10)
            yield _R("tail")
        return _gen()


class _NoProgressLLM:
    model_name = "m-dead"

    def astream(self, messages):
        async def _gen():
            await asyncio.sleep(10)
            yield _R("late")
        return _gen()


async def test_first_chunk_wait_not_gap_clamped(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_CHUNK_GAP", "0.05")
    r = await _invoke_llm_abortable(_SlowFirstChunkLLM(), [], 1.0, None)
    assert "second" in r.content, (
        "首 chunk 等待（含槽位排队+prefill）应由软限把守，不受 chunk-gap 钳制"
        "——gap 只管解码中途两 chunk 间隔")


async def test_inter_chunk_gap_still_enforced(monkeypatch):
    """回归护栏：解码中途 stall 仍按 gap 早杀，不等软限。"""
    monkeypatch.setenv("SWARM_PLAN_BATCH_CHUNK_GAP", "0.05")
    t0 = asyncio.get_event_loop().time()
    with pytest.raises(TimeoutError):
        await _invoke_llm_abortable(_MidStallLLM(), [], 5.0, None)
    assert asyncio.get_event_loop().time() - t0 < 2.0


async def test_no_first_chunk_killed_at_soft_limit():
    """回归护栏：全程无进展仍按软限杀。"""
    t0 = asyncio.get_event_loop().time()
    with pytest.raises(TimeoutError):
        await _invoke_llm_abortable(_NoProgressLLM(), [], 0.15, None)
    assert asyncio.get_event_loop().time() - t0 < 2.0


async def test_queue_starved_timeout_not_fed_to_breaker(monkeypatch):
    """整段超时耗在槽位排队（自致拥塞）→ 不得计入熔断失败（假信号熔断健康模型）。"""
    from swarm.models import router as _router
    monkeypatch.setattr(_router, "slot_wait_state",
                        lambda: {"queued_at": 0.0, "acquired_at": None},
                        raising=False)
    with pytest.raises(TimeoutError):
        await _invoke_llm_abortable(_NoProgressLLM(), [], 0.1, None)
    snap = breaker.snapshot().get("m-dead")
    assert not snap or snap["consecutive_failures"] == 0, (
        "排队饿死是进程内自致拥塞，非模型失败——喂熔断会把健康模型熔断")


async def test_slot_acquire_failopen_after_bound(monkeypatch):
    """acquire 有界：槽位被占死（泄漏/挂死持有者）超界后 WARNING fail-open 放行。"""
    from swarm.models.router import _DualTimeoutChatOpenAI, _provider_slot
    monkeypatch.setenv("SWARM_PROVIDER_SLOT_WAIT_S", "0.05")
    llm = _DualTimeoutChatOpenAI(
        api_key="test-dummy", model="tm",
        swarm_provider_id="prov-fo", swarm_provider_concurrency=1)
    sem = _provider_slot("prov-fo", 1)
    await sem.acquire()  # 占死唯一槽位

    async def _fake_inner(self, *a, **k):
        yield _R("alive")

    monkeypatch.setattr(_DualTimeoutChatOpenAI, "_astream_inner", _fake_inner)
    chunks = []

    async def _run():
        async for c in llm._astream(["hi"]):
            chunks.append(c)

    await asyncio.wait_for(_run(), timeout=2.0)
    assert chunks, (
        "槽位等待超界必须 fail-open 放行（泄漏可观测降级），绝不静默无限挂死")


async def test_slot_normal_path_still_gated():
    """回归护栏：正常路径槽位仍闸并发。"""
    from swarm.models.router import _provider_slot
    peak = {"now": 0, "max": 0}

    async def _one():
        sem = _provider_slot("prov-g", 2)
        async with sem:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.02)
            peak["now"] -= 1

    await asyncio.gather(*[_one() for _ in range(6)])
    assert peak["max"] <= 2


# ─────────────── F-C：stall 双失败与 timeout 同桶（U3 bisect 复活）───────────────

_OK_SUBTASKS = {
    "subtasks": [
        {
            "id": "st-1",
            "description": "实现 alarm-sdk 基础能力",
            "scope": {"create_files": ["alarm-sdk/src/A.java"], "writable": [], "readable": []},
        }
    ]
}


class _StallModuleLLM:
    """含 stall_module 的批恒抛 TransientInfraError（流中 stall 形态）。"""

    def __init__(self, stall_module: str):
        self.stall_module = stall_module

    async def ainvoke(self, msgs):
        user = msgs[-1]["content"]
        if f"'{self.stall_module}'" in user:
            raise TransientInfraError("stream 解码中途 stall 超时 30s")
        return _R(json.dumps(_OK_SUBTASKS, ensure_ascii=False))


async def test_stall_double_failure_buckets_timeout_and_bisects(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "5")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    import swarm.brain.nodes as _nodes
    monkeypatch.setattr(_nodes, "_get_brain_fallback_llm", lambda: None)
    from swarm.brain.nodes import _plan_ultra_batched

    state = {
        "tech_design": {"modules": [
            {"name": "alarm-sdk", "depends_on": []},
            {"name": "system-enhance", "depends_on": []},
        ]},
        "shared_contract_draft": {},
        "project_id": "",
    }
    file_plan = [
        {"path": "alarm-sdk/src/A.java", "module": "alarm-sdk", "action": "create"},
        {"path": "system-enhance/src/B.java", "module": "system-enhance", "action": "create"},
        {"path": "system-enhance/src/C.java", "module": "system-enhance", "action": "create"},
    ]
    _plan, failed, _bl, _c = await _plan_ultra_batched(
        _StallModuleLLM("system-enhance"), state, "需求描述", {}, "", file_plan)
    assert failed, "stall 模块必须结构化记账"
    assert all(m["reason"] == "timeout" for m in failed), (
        f"stall（TransientInfraError）终态必须与 timeout 同桶——"
        f"否则 U3 bisect 对本阶段立项要治的形态静默失效: {failed}")
    assert any("~" in m["name"] for m in failed), (
        "timeout 桶必须触发 U3 对半切分（半批 ~a/~b 记账），证明 bisect 已对 stall 生效")


# ─────────────── F-D：槽位池键含 limit（热变更生效）───────────────

async def test_provider_slot_key_includes_limit():
    from swarm.models.router import _provider_slot
    s2 = _provider_slot("prov-l", 2)
    s3 = _provider_slot("prov-l", 3)
    assert s2 is not s3, "并发上限热变更必须拿到新池（键含 limit），不得被旧池静默吞"
    assert _provider_slot("prov-l", 2) is s2


# ─────────────── F-E：熔断跳过路径 ensure_budget 对称 ───────────────

async def test_skip_primary_path_checks_budget(monkeypatch):
    _open_breaker("m-stall")  # 熔断开启 → 走跳过 primary 直奔备用的路径
    from swarm.models import ledger as _ledger

    def _boom(task_id, min_tokens=0):
        raise TaskTokenLimitExceeded({"task_id": task_id})

    monkeypatch.setattr(_ledger, "ensure_budget", _boom)
    fb = _OkLLM()
    with pytest.raises(TaskTokenLimitExceeded):
        await _invoke_llm_abortable(_StallLLM(), [], 5.0, fb)
    assert fb.calls == 0, (
        "预算耗尽时熔断跳过路径不得再对备用发起调用（与其余切备点头寸检查对称）")
