"""R65D-T6①：批门闩→滚动派发（round65d 8min stall 治本）。

round65d 实锤：dispatch 节点 as_completed 只做进度回写，节点仍收齐整批才返回图
循环——最慢 worker 扣住全批，后继就绪任务干等（观测 8min 空转）；消费边下推（#57）
让依赖链更长，批门闩的代价随之放大。

治本（滚动补位，护栏三条）：
- 批内任一任务完成即查新就绪者补位（get_dispatch_batch 同源选批：依赖闸/放弃集/
  fresh 优先/扇出全继承），槽位=max_concurrent；
- ★任一失败/异常立即停止补位、收批返回★——HANDLE_FAILURE/R13-4 批间熔断节奏原样保留；
- 单节点补位总量封顶 max_concurrent×SWARM_DISPATCH_ROLL_FACTOR（默认 3，0=关闭回
  旧批门闩语义）；超大块（_oversized_by_files）不滚动，留节点级预算闸拆小。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from swarm.brain.nodes.dispatch import dispatch
from swarm.types import Confidence, FileScope, SubTask, TaskPlan, WorkerOutput


def _subtask(sid, depends=None):
    return SubTask(id=sid, description=f"t {sid}",
                   scope=FileScope(writable=[f"src/{sid}.py"]),
                   depends_on=depends or [])


def _ok(sid, delay=0.02):
    async def _w():
        await asyncio.sleep(delay)
        return WorkerOutput(subtask_id=sid, diff=f"--- a\n+++ b\n+{sid}\n",
                            summary="ok", confidence=Confidence.HIGH,
                            l1_passed=True)
    return _w


def _mk_state(plan, remaining):
    return {"task_id": "t-roll", "project_id": "proj-1", "plan": plan,
            "subtask_results": {}, "dispatch_remaining": list(remaining),
            "failed_subtask_ids": [], "knowledge_context": {}}


def _fake(delay_map=None, fail_ids=()):
    delay_map = delay_map or {}

    async def fake_worker(subtask, knowledge_context, project_id="",
                          task_id="", **kwargs):
        await asyncio.sleep(delay_map.get(subtask.id, 0.02))
        if subtask.id in fail_ids:
            return WorkerOutput(subtask_id=subtask.id, diff="", summary="fail",
                                confidence=Confidence.LOW, l1_passed=False)
        return WorkerOutput(subtask_id=subtask.id,
                            diff=f"--- a\n+++ b\n+{subtask.id}\n",
                            summary="ok", confidence=Confidence.HIGH,
                            l1_passed=True)
    return fake_worker


@pytest.mark.asyncio
async def test_rolling_backfills_dependency_chain_in_one_node_call(monkeypatch):
    """★批门闩本体★：链 A→B→C 在【一次】dispatch 节点调用内滚动完成——
    旧行为一批只做 A（B/C 干等下一轮图循环=每级一整圈开销）。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "3")
    plan = TaskPlan(subtasks=[
        _subtask("st-a"),
        _subtask("st-b", depends=["st-a"]),
        _subtask("st-c", depends=["st-b"]),
    ], parallel_groups=[["st-a", "st-b", "st-c"]])
    state = _mk_state(plan, ["st-a", "st-b", "st-c"])
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=_fake()):
        r = await dispatch(state)
    assert set(r["subtask_results"].keys()) == {"st-a", "st-b", "st-c"}, \
        f"依赖链应在单节点调用内滚动完成: {sorted(r['subtask_results'])}"
    assert r["dispatch_remaining"] == []


@pytest.mark.asyncio
async def test_rolling_stops_on_failure(monkeypatch):
    """★熔断节奏护栏★：批内任一失败立即停止补位——B（依赖 A）绝不在失败后被
    滚动派发，收批交 HANDLE_FAILURE（批间熔断节奏原样）。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "3")
    plan = TaskPlan(subtasks=[
        _subtask("st-a"),
        _subtask("st-d"),
        _subtask("st-b", depends=["st-a"]),
    ], parallel_groups=[["st-a", "st-d", "st-b"]])
    state = _mk_state(plan, ["st-a", "st-d", "st-b"])
    # D 秒败、A 慢——失败先落地，A 完成时补位已被封锁
    fake = _fake(delay_map={"st-a": 0.15, "st-d": 0.0}, fail_ids={"st-d"})
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake):
        r = await dispatch(state)
    assert "st-d" in (r.get("failed_subtask_ids") or []), r
    assert "st-b" not in r["subtask_results"], \
        "失败后绝不滚动补位（保 HANDLE_FAILURE/R13-4 批间节奏）"
    assert "st-b" in r["dispatch_remaining"]


@pytest.mark.asyncio
async def test_rolling_capped_per_node_call(monkeypatch):
    """封顶护栏：单节点补位总量 ≤ max_concurrent×factor——长链绝不在一个节点里
    无界滚动（checkpoint 粒度/心跳观测面）。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "1")
    chain = [_subtask("st-0")]
    for i in range(1, 8):
        chain.append(_subtask(f"st-{i}", depends=[f"st-{i-1}"]))
    plan = TaskPlan(subtasks=chain, parallel_groups=[[s.id for s in chain]])
    state = _mk_state(plan, [s.id for s in chain])
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=_fake()), \
         patch("swarm.brain.nodes.dispatch.get_config") as _gc:
        _gc.return_value.worker.max_concurrent = 2
        _gc.return_value.model = __import__("swarm.config", fromlist=["x"]) \
            .get_config().model
        r = await dispatch(state)
    done = len(r["subtask_results"])
    # 复核 MED：精确锁死封顶值（链式场景首批只有 1 个根就绪，+补位 cap=2×1=2 → 恒 3；
    # 区间断言锁不住差一错误）
    assert done == 3, f"封顶必须精确（首批1+补位=cap 2）: {done}"


@pytest.mark.asyncio
async def test_roll_factor_zero_keeps_legacy_batch(monkeypatch):
    """回退开关：factor=0 完全恢复旧批门闩语义（一次节点调用只做首批）。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "0")
    plan = TaskPlan(subtasks=[
        _subtask("st-a"),
        _subtask("st-b", depends=["st-a"]),
    ], parallel_groups=[["st-a", "st-b"]])
    state = _mk_state(plan, ["st-a", "st-b"])
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=_fake()):
        r = await dispatch(state)
    assert set(r["subtask_results"].keys()) == {"st-a"}, \
        f"factor=0 必须保持旧行为: {sorted(r['subtask_results'])}"
    assert "st-b" in r["dispatch_remaining"]


# ───────────────── ③ 规划侧 C7 在飞预留作用域（TTL 泄漏治本） ─────────────────

@pytest.mark.asyncio
async def test_abortable_settles_leaked_reservations_on_timeout():
    """★round65d 09:52/09:57 泄漏本体★：规划调用被墙钟取消 → 在飞预留立即按中止
    语义结算（settle_leaked=True），绝不挂 30min TTL 虚增。"""
    from swarm.brain.nodes import _invoke_llm_abortable

    class _HangLLM:
        model_name = "hang-model"

        async def ainvoke(self, messages):
            await asyncio.sleep(10)

    with patch("swarm.models.ledger.begin_inflight_scope",
               return_value="tok-1") as _b, \
         patch("swarm.models.ledger.end_inflight_scope") as _e:
        with pytest.raises(asyncio.TimeoutError):
            await _invoke_llm_abortable(_HangLLM(), [], total_timeout=0.05)
    _b.assert_called_once()
    _e.assert_called_once_with("tok-1", settle_leaked=True)


@pytest.mark.asyncio
async def test_abortable_normal_return_no_leak_settle():
    """对照面：正常返回 settle_leaked=False（残留交 TTL 兜底，不误按中止结算）。"""
    from swarm.brain.nodes import _invoke_llm_abortable

    class _FastLLM:
        model_name = "fast-model"

        async def ainvoke(self, messages):
            return type("R", (), {"content": "ok"})()

    with patch("swarm.models.ledger.begin_inflight_scope",
               return_value="tok-2"), \
         patch("swarm.models.ledger.end_inflight_scope") as _e:
        r = await _invoke_llm_abortable(_FastLLM(), [], total_timeout=5)
    assert getattr(r, "content", "") == "ok"
    _e.assert_called_once_with("tok-2", settle_leaked=False)


@pytest.mark.asyncio
async def test_rolled_dispatches_counted_in_lifetime_totals(monkeypatch):
    """★双复核 CRITICAL/HIGH 锁★：滚动补位者必须计入 subtask_dispatch_totals
    终身账（A2 硬熔断唯一数据源）且其 use_alternate 标记被消费即清。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "3")
    plan = TaskPlan(subtasks=[
        _subtask("st-a"),
        _subtask("st-b", depends=["st-a"]),
    ], parallel_groups=[["st-a", "st-b"]])
    state = _mk_state(plan, ["st-a", "st-b"])
    state["subtask_use_alternate"] = {"st-b": True}
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=_fake()):
        r = await dispatch(state)
    totals = r.get("subtask_dispatch_totals") or {}
    assert totals.get("st-b") == 1, \
        f"滚动派发必须进终身账（否则 A2 硬熔断对滚动者永不触发）: {totals}"
    assert "st-b" not in (r.get("subtask_use_alternate") or {}), \
        "滚动派发同样消费即清 alternate（防粘滞劫持路由）"


@pytest.mark.asyncio
async def test_rolled_worker_exception_stops_rolling(monkeypatch):
    """复核 MED 盲区锁：滚动补位者抛异常（非 l1_passed=False）同样触发停滚收批。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "3")
    plan = TaskPlan(subtasks=[
        _subtask("st-a"),
        _subtask("st-b", depends=["st-a"]),
        _subtask("st-c", depends=["st-b"]),
    ], parallel_groups=[["st-a", "st-b", "st-c"]])
    state = _mk_state(plan, ["st-a", "st-b", "st-c"])

    async def fake(subtask, knowledge_context, project_id="", task_id="", **kw):
        await asyncio.sleep(0.01)
        if subtask.id == "st-b":
            raise RuntimeError("boom on rolled task")
        return WorkerOutput(subtask_id=subtask.id,
                            diff=f"--- a\n+++ b\n+{subtask.id}\n",
                            summary="ok", confidence=Confidence.HIGH,
                            l1_passed=True)

    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake):
        r = await dispatch(state)
    assert "st-b" in (r.get("failed_subtask_ids") or []), r
    assert "st-c" not in r["subtask_results"], \
        "滚动者异常后绝不再补位（异常与 L1 失败同样停滚）"


@pytest.mark.asyncio
async def test_rolling_continues_past_fast_blocked(monkeypatch):
    """R65REPLAY-T5（回放末段 26min 并发≈1 死型）：seed 闸 BLOCKED 秒退（零资源
    消耗/零树改动/秒级返回）不冻结补位——批内全是秒退者+一个 900s 长尾时，补位
    一票冻结把机群烧成单线程。真失败（烧预算/坏产物）仍立即冻结（既有护栏不动）。
    形态：初始批 [st-a(慢成功), st-blk(秒 BLOCKED)]；st-e 依赖 st-a——st-a 完成后
    补位必须轮到 st-e，不被 st-blk 的秒退挡死。BLOCKED 者仍入 failed 交 HANDLE_FAILURE。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "3")
    plan = TaskPlan(subtasks=[
        _subtask("st-a"),
        _subtask("st-blk"),
        _subtask("st-e", depends=["st-a"]),
    ], parallel_groups=[["st-a", "st-blk", "st-e"]])
    state = _mk_state(plan, ["st-a", "st-blk", "st-e"])

    async def fake_worker(subtask, knowledge_context, project_id="",
                          task_id="", **kwargs):
        if subtask.id == "st-blk":
            return WorkerOutput(
                subtask_id="st-blk", diff="",
                summary="[#12·seed闸门] 上游产物缺失，判 BLOCKED 等生产者",
                confidence=Confidence.LOW, l1_passed=False,
                l1_details={"pipeline_blocked": "internal_pkg_not_built",
                            "not_run_kind": "blocked",
                            "failure_class": "transient",
                            "blocked_stage": "preflight",
                            "blocked_on_files": ["m/X.java"]})
        await asyncio.sleep(0.15 if subtask.id == "st-a" else 0.02)
        return WorkerOutput(subtask_id=subtask.id,
                            diff=f"--- a\n+++ b\n+{subtask.id}\n",
                            summary="ok", confidence=Confidence.HIGH,
                            l1_passed=True)

    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        r = await dispatch(state)
    assert "st-e" in r["subtask_results"], \
        f"BLOCKED 秒退冻结补位=回放末段并发 1 死型: {sorted(r['subtask_results'])}"
    assert "st-blk" in (r.get("failed_subtask_ids") or []), \
        "BLOCKED 者仍须收批交 HANDLE_FAILURE（只解冻补位，不吞失败处置）"


@pytest.mark.asyncio
async def test_rolling_still_stops_on_real_failure_with_blocked_mix(monkeypatch):
    """对照锁：混批里出现【真失败】（非 BLOCKED 秒退）→ 补位仍立即冻结。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "3")
    plan = TaskPlan(subtasks=[
        _subtask("st-a"),
        _subtask("st-real"),
        _subtask("st-e", depends=["st-a"]),
    ], parallel_groups=[["st-a", "st-real", "st-e"]])
    state = _mk_state(plan, ["st-a", "st-real", "st-e"])
    fake = _fake(delay_map={"st-a": 0.15, "st-real": 0.0}, fail_ids={"st-real"})
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake):
        r = await dispatch(state)
    assert "st-e" not in r["subtask_results"], "真失败必须冻结补位（护栏不回归）"


@pytest.mark.asyncio
async def test_rolling_freezes_on_expensive_blocked_without_preflight(monkeypatch):
    """复核 F1 HIGH 锁：昂贵 BLOCKED（真 build 后 internal_pkg_not_built/烧满预算
    超时——同带 not_run_kind=blocked+failure_class=transient 但【无 preflight 标记】）
    仍必须冻结补位——绝不让"披着 BLOCKED 外衣的真失败"放行补位风暴。"""
    monkeypatch.setenv("SWARM_DISPATCH_ROLL_FACTOR", "3")
    plan = TaskPlan(subtasks=[
        _subtask("st-a"),
        _subtask("st-exp"),
        _subtask("st-e", depends=["st-a"]),
    ], parallel_groups=[["st-a", "st-exp", "st-e"]])
    state = _mk_state(plan, ["st-a", "st-exp", "st-e"])

    async def fake_worker(subtask, knowledge_context, project_id="",
                          task_id="", **kwargs):
        if subtask.id == "st-exp":
            return WorkerOutput(
                subtask_id="st-exp", diff="",
                summary="真 build 5min 后 internal_pkg_not_built（昂贵 BLOCKED）",
                confidence=Confidence.LOW, l1_passed=False,
                l1_details={"pipeline_blocked": "internal_pkg_not_built",
                            "not_run_kind": "blocked",
                            "failure_class": "transient"})   # 无 blocked_stage
        await asyncio.sleep(0.15 if subtask.id == "st-a" else 0.02)
        return WorkerOutput(subtask_id=subtask.id,
                            diff=f"--- a\n+++ b\n+{subtask.id}\n",
                            summary="ok", confidence=Confidence.HIGH,
                            l1_passed=True)

    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        r = await dispatch(state)
    assert "st-e" not in r["subtask_results"],         "昂贵 BLOCKED 凭共用标记对豁免=补位风暴（每个候选再烧 5min build）"
