"""阶段3.9 对抗复核（silent-failure-hunter）治理批：F1-F8 行为锁。

猎手 8 条 finding 逐条亲核后全 CONFIRMED（F7 由 PLAUSIBLE 升级——dispatch 对失败撮
降优先级，首批大概率是无关新前沿，消费即清恰好把 alternate 路由送错人）：
  F1 F8 分桶结构性遮蔽 A7 候选块——A7 在它为之而生的 ULTRA 分批路径上是死代码。
  F2 fetch_structure_inventory 破坏 KB-loop 亲和契约（全文件唯一直连者）+ 无超时
     → 间歇性哑火（跨线程 psycopg 锁）或 plan 节点无限期挂死（连接半死无异常）。
  F3 阶段3 自己把 sliding_ctx 头部撑爆 [:14000]（wm 块 7K + 分页 feedback 7.5K +
     修补块 9.5K ≈ 24K）→ 增量修补纪律+D1 硬约束整块被截没（round31 H-1 同族回归）。
  F4 A7 零候选两个静默形态：索引未建零日志；4000/8000 截断把大仓合法申报判死不自述。
  F5 plan_coverage:gap_allowed 进 append-only degraded_reasons 无人能清——缺口后来
     补齐仍永久拦 L6 + deliver 展示陈旧缺口。治=独立 last-write-wins 键
     coverage_gap_residual（消费面 = should_write_success + deliver payload）。
  F6 F10 签名在 plan_valid=False 时也发——硬 LLM 门/完整性预算>1 下「原样重交=自动过闸」。
  F7 use_alternate_model 全局 bool 单批消费送错人——改按子任务记账 subtask_use_alternate。
  F8 横切提示 [:50] 截断不自述（违反本仓截断必自述纪律）。
"""

from __future__ import annotations

import asyncio
import json
import typing
from unittest.mock import patch


from swarm.types import (
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


class _R:
    def __init__(self, content):
        self.content = content


class _CaptureLLM:
    """记录每批 user prompt 的假 LLM（复用 test_batch_req_bucketing_f8_a10 骨架）。"""

    def __init__(self):
        self.prompts: list[str] = []

    async def ainvoke(self, msgs):
        user = msgs[-1]["content"]
        self.prompts.append(user)
        if "'alarm-sdk'" in user:
            f = "alarm-sdk/src/AlarmService.java"
        else:
            f = "user-center/src/UserController.java"
        return _R(json.dumps({"subtasks": [{
            "id": "st-1", "description": f"impl {f}",
            "scope": {"create_files": [f], "writable": [], "readable": []},
        }]}, ensure_ascii=False))


def _batched_state(req_items):
    return {
        "tech_design": {"modules": [
            {"name": "alarm-sdk", "depends_on": []},
            {"name": "user-center", "depends_on": []},
        ]},
        "shared_contract_draft": {},
        "project_id": "proj-1",
        "requirement_items": req_items,
    }


_FILE_PLAN = [
    {"path": "alarm-sdk/src/AlarmService.java", "module": "alarm-sdk", "action": "create"},
    {"path": "user-center/src/UserController.java", "module": "user-center", "action": "create"},
]


# ─────────────── F1：分桶成功时 A7 候选块必须仍在每批 prompt ───────────────

async def test_f1_a7_block_survives_f8_bucketing(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "5")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    import swarm.brain.nodes as _nodes
    import swarm.knowledge.service as _ksvc
    monkeypatch.setattr(_nodes, "_get_brain_fallback_llm", lambda: None)

    async def _fake_inventory(pid, max_files=4000, max_symbols=8000):
        # AlarmService stem 与需求文本 ASCII token 命中（==2 ≥ 阈值 2.0）→ 必产候选
        return ([{"file_path": "alarm-sdk/src/AlarmService.java",
                  "module_name": "alarm-sdk"}], [])

    monkeypatch.setattr(_ksvc, "fetch_structure_inventory", _fake_inventory)
    from swarm.brain.nodes import _plan_ultra_batched
    llm = _CaptureLLM()
    state = _batched_state([
        {"id": "req-a1", "text": "AlarmService 告警发送能力"},
        {"id": "req-b1", "text": "user-center 支持按部门筛选"},
    ])
    await _plan_ultra_batched(llm, state, "需求", {}, "", list(_FILE_PLAN))
    assert llm.prompts, "分批路径必须实际发批"
    for p in llm.prompts:
        assert "存量候选对账清单" in p, (
            "F8 分桶成功时 A7 候选块被结构性遮蔽（.get() 命中每模块块，带 A7 的"
            " fallback 永不使用）——A7 在 ULTRA 分批主战场成死代码")


# ─────────────── F2：A7 结构清单必须跑在 KB loop + 有界超时 ───────────────

async def test_f2_inventory_runs_on_kb_loop(monkeypatch):
    import swarm.knowledge.service as svc
    seen: dict = {}

    class _Idx:
        async def list_inventory(self, pid, mf, ms):
            seen["loop"] = asyncio.get_running_loop()
            return [], []

    class _Retriever:
        _struct = _Idx()

    async def _fake_get_retriever():
        return _Retriever()

    monkeypatch.setattr(svc, "get_retriever", _fake_get_retriever)
    files, symbols = await svc.fetch_structure_inventory("p1")
    assert files == [] and symbols == []
    assert seen["loop"] is svc._get_kb_loop(), (
        "唯一直连 get_retriever 的入口——跨线程触碰 KB loop 的 psycopg 连接，"
        "间歇性 RuntimeError 被吞成零候选/半死连接下 plan 节点无限挂死")


async def test_f2_inventory_bounded_wait(monkeypatch):
    import swarm.knowledge.service as svc
    monkeypatch.setenv("SWARM_KB_SYNC_TIMEOUT_SEC", "1")

    async def _hang():
        await asyncio.sleep(999)

    monkeypatch.setattr(svc, "get_retriever", _hang)
    t0 = asyncio.get_running_loop().time()
    files, symbols = await svc.fetch_structure_inventory("p1")
    assert (files, symbols) == ([], []), "超时必须降级为空候选（fail-open），不许挂死"
    assert asyncio.get_running_loop().time() - t0 < 10, "必须有界等待（对齐 D15 惯例）"


# ─────────────── F4：索引未建留痕 + 截断自述 ───────────────

async def test_f4_missing_index_logs_warning(monkeypatch, caplog):
    import logging

    import swarm.knowledge.service as svc

    class _Retriever:
        _struct = None

    async def _fake_get_retriever():
        return _Retriever()

    monkeypatch.setattr(svc, "get_retriever", _fake_get_retriever)
    with caplog.at_level(logging.WARNING, logger="swarm.knowledge.service"):
        files, symbols = await svc.fetch_structure_inventory("p1")
    assert (files, symbols) == ([], [])
    assert any("索引未建" in r.message for r in caplog.records), (
        "「预处理没跑」与「绿地真没存量」必须可区分——降级可观测纪律")


def test_f4_truncated_inventory_self_describes():
    from swarm.brain.baseline_candidates import baseline_candidates_prompt_block
    cands = [{"id": "req-1", "text": "t", "candidates": [{"file": "a/B.java"}]}]
    blk = baseline_candidates_prompt_block(cands, truncated=True)
    assert "截断" in blk and "允许" in blk, (
        "4000/8000 截断下「清单外不要申报」从少提示升级为主动禁止合法申报——必须自述并放开")
    blk2 = baseline_candidates_prompt_block(cands)
    assert "清单外的条目不要凭空申报" in blk2, "未截断时纪律原样保留"


# ─────────────── F3：sliding_ctx 结构块头部必须在分批 prompt 存活 ───────────────

async def test_f3_repair_block_survives_batched_truncation(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "5")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    import swarm.brain.nodes as _nodes
    monkeypatch.setattr(_nodes, "_get_brain_fallback_llm", lambda: None)
    from swarm.brain.nodes import _plan_ultra_batched
    llm = _CaptureLLM()
    # 最坏头部实测 ~24K（分页 feedback 7.5K + wm 块 7.1K + 修补块 9.2K）——修补纪律
    # 是头部【最后】一段，[:14000] 恰好把它整块截没（round31 H-1 同族）。
    head = ("x" * 23800) + "\n【增量修补纪律哨兵】已完成子任务 covers 是硬约束\n"
    sliding = head + ("y" * 8000)
    await _plan_ultra_batched(
        llm, _batched_state([]), "需求", {}, sliding, list(_FILE_PLAN))
    assert llm.prompts
    assert any("增量修补纪律哨兵" in p for p in llm.prompts), (
        "头部 24K 的修补纪律块被分批路径截没→重试轮 LLM 看不到『不要全量重拆』"
        "→全量重拆→水位闸硬拒→白烧 MAX_PLAN_RETRY 的结构性烧钱环")


# ─────────────── F5：gap 残差独立键（last-write-wins），不再永久拦 L6 ───────────────

def _req(n):
    return [{"id": f"req-{i:08x}", "text": f"需求{i}", "kind": "feature"} for i in range(n)]


def _plan_covering(ids):
    subs = [SubTask(id=f"st-{i}", description=f"做{i}",
                    difficulty=SubTaskDifficulty.MEDIUM,
                    scope=FileScope(writable=[f"f{i}.py"], readable=[]),
                    covers=[rid]) for i, rid in enumerate(ids)]
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


async def _run_validate(state):
    from swarm.brain.nodes import validate_plan
    return await validate_plan(state)


async def test_f5_gap_pass_emits_residual_not_degraded(monkeypatch):
    monkeypatch.setenv("SWARM_VALIDATE_PLAN_LLM_GATE", "false")
    items = _req(10)
    covered = [it["id"] for it in items[:9]]  # 缺 1 条 ≤ max(2, 0.3)
    state = {
        "plan": _plan_covering(covered),
        "requirement_items": items,
        "plan_retry_count": 1,  # A6 条件：已给过修补机会
        "task_description": "t",
    }
    out = await _run_validate(state)
    assert out["plan_valid"] is True
    assert out.get("coverage_gap_residual") == [items[9]["id"]], (
        "残差必须走独立 last-write-wins 键（消费面=deliver+L6），append-only degraded 无人能清")
    assert not any("gap_allowed" in str(r) for r in (out.get("degraded_reasons") or [])), (
        "不再往 append-only 通道写 gap——后续补齐后陈旧留痕永久拦 L6")


async def test_f5_full_coverage_clears_residual(monkeypatch):
    monkeypatch.setenv("SWARM_VALIDATE_PLAN_LLM_GATE", "false")
    items = _req(4)
    state = {
        "plan": _plan_covering([it["id"] for it in items]),
        "requirement_items": items,
        "plan_retry_count": 2,
        "task_description": "t",
        "coverage_gap_residual": ["req-stale"],  # 上一轮 gap 放行的残差
    }
    out = await _run_validate(state)
    assert out["plan_valid"] is True
    assert out.get("coverage_gap_residual") == [], (
        "全覆盖过闸必须清残差——缺口已补齐还拦 L6/deliver 展示陈旧缺口=自相矛盾")


def test_f5_residual_blocks_l6():
    from swarm.memory.pattern_extractor import should_write_success
    state = {"complexity": "complex", "degraded_reasons": [],
             "coverage_gap_residual": ["req-1"]}
    assert should_write_success(state) is False, (
        "真实残差必须继续拦 L6 假成功学习（阻断语义从 degraded 迁到独立键，不放松）")


# ─────────────── F6：软校验签名只在真放行时发 ───────────────

async def test_f6_no_sig_on_hard_gate_reject(monkeypatch):
    import swarm.brain.nodes as nodes
    monkeypatch.setenv("SWARM_VALIDATE_PLAN_LLM_GATE", "true")

    class _RejectLLM:
        async def ainvoke(self, msgs):
            return _R('{"valid": false, "issues": ["质量差"]}')

    async def _fake_abortable(llm, msgs, timeout, ledger, node_label=""):
        return await llm.ainvoke(msgs)

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _RejectLLM())
    monkeypatch.setattr(nodes, "_invoke_llm_abortable", _fake_abortable)
    items = _req(2)
    state = {
        "plan": _plan_covering([it["id"] for it in items]),
        "requirement_items": items,
        "plan_retry_count": 0,
        "task_description": "t",
    }
    out = await _run_validate(state)
    assert out["plan_valid"] is False
    assert not out.get("plan_soft_review_sig"), (
        "否决轮发签名=下一轮原样结构跳检自动过闸（硬门被静默转放行）；只在真放行时 emit")


# ─────────────── F7：alternate 按子任务记账，不再全局 bool 单批消费 ───────────────

def test_f7_state_key_replaced():
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState
    hints = typing.get_type_hints(BrainState, include_extras=True)
    assert "subtask_use_alternate" in hints and \
        "subtask_use_alternate" in ACCOUNTING_KEY_LIFECYCLE
    assert "use_alternate_model" not in hints, (
        "全局 bool 语义已被按子任务映射替代——两个事实源并存必然漂移")


def test_f7_failure_marks_failed_subtasks(monkeypatch):
    """SIMPLE 快路 forced_alternate：alternate 决策必须落在失败子任务身上。"""
    from swarm.brain.nodes import handle_failure
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="a", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["a.py"], readable=[])),
    ], parallel_groups=[["st-1"]])
    state = {
        "complexity": "simple",
        "plan": plan,
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": WorkerOutput(
            subtask_id="st-1", diff="", summary="", l1_passed=False,
            confidence=Confidence.LOW)},
        "subtask_retry_counts": {"st-1": 2},  # max_retries=2 → 本次 deepest=3 触发 alternate
        "dispatch_remaining": [],
    }
    out = asyncio.run(handle_failure(state))
    assert out["failure_strategy"] == "retry_alternate"
    assert out.get("subtask_use_alternate", {}).get("st-1") is True, (
        "决策针对失败撮却记在全局 bool 上——首个 dispatch 批（大概率是被降优先级"
        "错开的无关新前沿）消费即清，真正重试的失败子任务反拿主力模型")


async def test_f7_dispatch_consumes_per_subtask(monkeypatch):
    """alternate 标记：本批派到的子任务消费即清；未派到的保留给后续批。"""
    from swarm.brain.nodes.dispatch import dispatch
    plan = TaskPlan(subtasks=[
        SubTask(id="st-new", description="新前沿", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["a.py"], readable=[]), depends_on=[]),
        SubTask(id="st-fail", description="失败重试", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["b.py"], readable=[]), depends_on=[]),
    ], parallel_groups=[["st-new", "st-fail"]])
    routed: dict[str, bool] = {}

    async def fake_worker(subtask, knowledge_context, project_id="", task_id="", **kw):
        return WorkerOutput(subtask_id=subtask.id, diff="+x\n", summary="",
                            l1_passed=True, confidence=Confidence.HIGH)

    import importlib
    # swarm.brain.nodes 包属性 dispatch 被同名函数遮蔽（api/__init__ 遮蔽同族坑）——用 importlib
    dmod = importlib.import_module("swarm.brain.nodes.dispatch")
    _orig = dmod._select_pool_override

    def spy_override(difficulty, idx, pool, ua, fs, strong):
        # 记录每个子任务的 alternate 位——按调用序与 to_dispatch 对齐
        routed[f"call-{len(routed)}"] = ua
        return _orig(difficulty, idx, pool, ua, fs, strong)

    monkeypatch.setattr(dmod, "_select_pool_override", spy_override)
    # 单模型池会按设计禁用 alternate（改走 recursion_boost）——测试固定双模型池
    from swarm.config.settings import get_config
    _cfg = get_config()
    monkeypatch.setattr(_cfg.worker, "worker_parallel_pool", ["m-a", "m-b"], raising=False)
    state = {
        "task_id": "t1", "project_id": "p1", "plan": plan,
        "subtask_results": {}, "dispatch_remaining": ["st-new", "st-fail"],
        "failed_subtask_ids": [], "knowledge_context": {},
        "subtask_use_alternate": {"st-fail": True},
        # st-fail 有重试史 → 降优先级（F7 病理核心：失败撮不一定在首批）
        "subtask_retry_counts": {"st-fail": 1},
    }
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        out = await dispatch(state)
    # 两个都派出（max_concurrent 默认 ≥2）：st-fail 拿 alternate，st-new 不拿
    assert out.get("subtask_use_alternate") == {}, "已派出的子任务标记消费即清"
    assert any(routed.values()), "失败子任务必须真的吃到 alternate 路由"
    assert not all(routed.values()), "无关新前沿绝不搭车 alternate"


# ─────────────── F8：横切提示截断自述 ───────────────

async def test_f8_cross_note_truncation_self_describes(monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "5")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    import swarm.brain.nodes as _nodes
    monkeypatch.setattr(_nodes, "_get_brain_fallback_llm", lambda: None)
    from swarm.brain.nodes import _plan_ultra_batched
    llm = _CaptureLLM()
    # 55 条纯横切（无任何模块 affinity）→ [:50] 截断必须自述剩余 5 条
    items = [{"id": f"req-x{i:04d}", "text": f"全局幂等约束{i}"} for i in range(55)]
    await _plan_ultra_batched(llm, _batched_state(items), "需求", {}, "", list(_FILE_PLAN))
    assert llm.prompts
    assert any("另有 5 条" in p for p in llm.prompts), (
        "第 51+ 条横切条目拿不到任务级认领提示又不自述——认领率静默下降白烧重试")
