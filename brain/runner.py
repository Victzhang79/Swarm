"""Brain 任务运行器 — 打通 create_task → Brain 执行 → interrupt → resume 主链路

职责:
- 单例 Brain graph（共享 MemorySaver，支持跨请求 resume）
- 后台执行 Brain 状态机，SSE 推送进度
- approve / revise / reject 通过 Command(resume=...) 恢复图执行
- 同步更新 task_records 状态
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections import deque
from typing import Any

from langgraph.types import Command

from swarm.audit import audit
from swarm.brain.graph import get_compiled_brain_graph
from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.project import store
from swarm.types import HumanDecision

logger = logging.getLogger(__name__)


class TaskTokenLimitExceeded(Exception):
    """单任务 token 估算超过 SWARM_MAX_TASK_TOKENS。"""

    def __init__(self, usage: dict[str, Any]):
        self.usage = usage
        super().__init__(f"token limit exceeded: {usage.get('total')}")


class TaskLockLost(Exception):
    """运行中模块锁在 Redis 侧丢失（续期返回 False）——防同模块并发写，fail-fast 中止（2nd#2）。"""

    def __init__(self, lock_key: str):
        self.lock_key = lock_key
        super().__init__(f"模块锁丢失: {lock_key}")


class TaskWallclockExceeded(Exception):
    """单次 Brain 执行墙钟超时（P1-B）——防失控任务无上限占沙箱/GPU。

    经 run_task/resume 的 `except Exception` 归一化为 FAILED，finally 释放锁/沙箱/_task_running。
    """

    def __init__(self, deadline_s: float, elapsed_s: float):
        self.deadline_s = deadline_s
        self.elapsed_s = elapsed_s
        super().__init__(f"任务墙钟超时（已跑 {elapsed_s:.0f}s > 上限 {deadline_s:.0f}s）")


def _effective_deadline_s(base_s: float, per_subtask_s: float, subtask_count: int | None) -> float:
    """P1-B 弹性预算：有效墙钟上限 = base + per_subtask×子任务数。

    ★防误杀大型任务★：base<=0 时返回 0（关闭）。否则随任务规模线性放宽——小任务只用 base，
    大任务（多子任务）按比例获得更多时间（合法 E2E 大任务实测 7-8h，弹性后达 20h+ 不会被斩）。
    subtask_count 在规划前未知（None→按 0 算，只用 base；规划揭示后由调用方动态重算放宽）。
    """
    if base_s <= 0:
        return 0.0
    n = subtask_count if (subtask_count and subtask_count > 0) else 0
    return base_s + max(0.0, per_subtask_s) * n


def _raise_if_wallclock_exceeded(start_monotonic: float, deadline_s: float) -> None:
    """P1-B：单次执行段超（弹性）墙钟上限 → raise TaskWallclockExceeded。deadline_s<=0 关闭。"""
    if deadline_s > 0:
        elapsed = time.monotonic() - start_monotonic
        if elapsed > deadline_s:
            raise TaskWallclockExceeded(deadline_s, elapsed)

# P2-F：SSE/WS fanout 有界参数（防慢/挂死客户端无界积压）。可经环境变量调。
_SUB_QUEUE_MAXSIZE = max(64, int(os.environ.get("SWARM_SSE_SUB_QUEUE_MAX", "2000")))
_MAX_SUBS_PER_TASK = max(1, int(os.environ.get("SWARM_SSE_MAX_SUBS_PER_TASK", "50")))


class _FanoutTopic:
    """单 task 的进度事件【发布-订阅】主题（N-CW1/N-CW2 根因修复）。

    旧实现：每 task 一个 asyncio.Queue 单消费者。SSE+WS 同开会争抢 queue.get() 各取一半→
    两端都丢进度；retry 期 register 覆盖队列对象→在途消费者孤儿化；断开不注销→内存涨。

    新实现：生产者 publish() 扇出到【每个订阅者各自的队列】；保留有界历史，late 订阅者订阅
    时回放历史（保持"任务先跑、SSE 后连仍能看到先前进度"的语义）。订阅者断开 unsubscribe()
    即回收其队列。生产侧 API（_emit/register/get）不变，仅语义升级。
    """

    __slots__ = ("_subs", "_history")

    def __init__(self, history: int = 500) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        # P2-F：订阅者队列【有界】——慢/挂死的 SSE 客户端不再让队列无界增长撑爆内存。
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUB_QUEUE_MAXSIZE)
        # 复核 F3：history 长于队列容量时，回放【最近 maxsize 条】而非最旧那批——否则 late 订阅者
        # 填满队列的全是陈旧事件、错过重建当前状态所需的最新进度。deque 为旧→新，取尾部切片。
        hist = list(self._history)
        if q.maxsize and len(hist) > q.maxsize:
            hist = hist[-q.maxsize:]
        for ev in hist:  # 回放历史，late 订阅者也能看到先前进度
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                break
        # P2-F：订阅者数软上限——超限仅告警（可观测）不硬拒（SSE/WS 仍可连），
        # 每队列已有界故总内存 = N×maxsize 受控；异常多订阅者=泄漏信号，需人工排查。
        if len(self._subs) >= _MAX_SUBS_PER_TASK:
            logger.warning("[FANOUT] 单 task 订阅者数达软上限 %d，疑似 SSE/WS 未注销泄漏",
                           _MAX_SUBS_PER_TASK)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        self._history.append(event)
        for q in list(self._subs):  # 扇出到每个订阅者各自队列（put_nowait 不阻塞）
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # P2-F：队列满（慢消费者）→ drop-oldest 保最新进度，不阻塞、不 OOM、不误伤其它订阅者。
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def __bool__(self) -> bool:  # 兼容旧 `if queue:` 判断
        return True


# task_id → 进度事件 fanout 主题（pub/sub 多订阅扇出）
_task_queues: dict[str, _FanoutTopic] = {}

# task_id → 是否正在执行（防止重复 resume）
_task_running: set[str] = set()

# task_id → asyncio.Task 句柄（用于 cancel）
_task_handles: dict[str, asyncio.Task] = {}

# DB 中视为“进行中”的状态（API 重启后可能 orphaned）。
# 单一事实源见 swarm/task_states.py：并集含 CLARIFYING/DESIGN_REVIEW（修 P0-D cancel 死区）。
from swarm.task_states import (  # noqa: E402
    ACTIVE_DB_STATUSES as _ACTIVE_DB_STATUSES,
    ACTIVE_EXECUTION_STATES as _ACTIVE_EXECUTION_STATES,
    INTERRUPT_SUSPENDED_STATES as _INTERRUPT_SUSPENDED_STATES,
    TERMINAL_STATES as _TERMINAL_STATES,
)

# Brain 节点 → 任务状态 / UI 阶段
_NODE_STATUS_MAP: dict[str, str] = {
    "analyze": "ANALYZING",
    "plan": "PLANNING",
    "validate_plan": "VALIDATING_PLAN",
    "confirm": "CONFIRMING",
    "dispatch": "DISPATCHING",
    "monitor": "MONITORING",
    "handle_failure": "HANDLING_FAILURE",
    "merge": "MERGING",
    "verify_l2": "VERIFYING_L2",
    "verify_l3": "VERIFYING_L3",
    "deliver": "DELIVERING",
    "revision": "IN_REVISION",
    "learn_success": "LEARNING_SUCCESS",
    "learn_failure": "LEARNING_FAILURE",
}

# 需要在人工审核处暂停的 interrupt 类型
# 治本(task 661ecacb)：补上 clarify_fact_issue——TECH_DESIGN 事实核验检出虚假前提后，交互模式
# 走 interrupt({"type":"clarify_fact_issue"})。此前它不在本集合 → runner 不 surfaced →
# --no-auto-accept 下任务在 clarify 处静默暂停、前端无提示 → 死等。补进来才能让用户答复。
_REVIEW_INTERRUPT_TYPES = frozenset(
    {"deliver", "confirm_plan", "clarify", "clarify_fact_issue", "review_design"}
)

# interrupt 类型 → (任务状态, 人类可读标签)
_INTERRUPT_STATUS_LABEL = {
    "confirm_plan": ("CONFIRMING", "计划确认"),
    "deliver": ("DELIVERING", "结果审核"),
    "clarify": ("CLARIFYING", "需求澄清"),
    "clarify_fact_issue": ("CLARIFYING", "需求澄清（虚假前提）"),
    "review_design": ("DESIGN_REVIEW", "技术方案评审"),
}


def get_task_queue(task_id: str) -> _FanoutTopic | None:
    return _task_queues.get(task_id)


def register_task_queue(task_id: str) -> _FanoutTopic:
    # N-CW1：幂等——已存在则【复用】同一主题，绝不覆盖（否则 retry/revise 会孤儿化在途订阅者）。
    topic = _task_queues.get(task_id)
    if topic is None:
        topic = _FanoutTopic()
        _task_queues[task_id] = topic
        _cleanup_old_queues()
    return topic


def subscribe_task(task_id: str) -> tuple[_FanoutTopic, asyncio.Queue[dict[str, Any]]]:
    """消费者（SSE/WS）订阅某 task 的进度，返回 (主题, 专属队列)。用完须 unsubscribe。"""
    topic = get_task_queue(task_id) or register_task_queue(task_id)
    return topic, topic.subscribe()


def _cleanup_old_queues() -> None:
    if len(_task_queues) > 200:
        for key in list(_task_queues.keys())[: len(_task_queues) - 100]:
            if key not in _task_running:
                _task_queues.pop(key, None)


async def _emit(topic: _FanoutTopic, event: dict[str, Any]) -> None:
    # 扇出到所有订阅者各自队列（非阻塞）；无订阅者时仅入历史，供 late 订阅者回放。
    topic.publish(event)


def _emit_task_notification(task_id: str, task_rec: dict[str, Any], status: str) -> None:
    """写入应用内通知（任务完成/失败，带 task_id）。失败不影响主流程。"""
    try:
        desc = (task_rec.get("description") or "")[:80]
        if status == "DONE":
            title, etype = "任务已完成", "task_completed"
        elif status == "FAILED":
            title, etype = "任务失败", "task_failed"
        else:
            title, etype = "任务更新", "task_updated"
        store.create_notification(
            etype,
            task_id=task_id,
            project_id=task_rec.get("project_id"),
            title=title,
            message=f"#{task_id[:8]} {desc}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[RUNNER] 写通知失败: %s", exc)


def _set_workspace(project_id: str) -> None:
    try:
        project = store.get_project(project_id)
        if project and project.get("path"):
            # M2 修复：用 ContextVar 设工作根（并发隔离），不再裸写进程级 os.environ
            from swarm.tools.paths import set_workspace_root
            set_workspace_root(project["path"])
            logger.info("[RUNNER] workspace → %s", project["path"])
    except Exception as exc:
        logger.warning("[RUNNER] 设置 workspace 失败: %s", exc)


# 进度/产物回写触发节点（on_chain_end）。#8 治本：补上 "elaborate"——PLAN 生成 N 子任务后，
# ELABORATE 二次拆分把 N 放大(如 35→64)并 `out["plan"]` 回写 state，但原列表漏了 elaborate →
# subtask_count 分母停在拆分前 N → WebUI 显示 0/N 假分母。_sync_task_from_state 收的是节点 output
# 增量，dispatch 虽在列表但其 output 不含 "plan" 键，救不回分母；必须在 elaborate 结束即回写。
_SYNC_ON_NODES = ("analyze", "plan", "elaborate", "merge", "verify_l3", "dispatch")
# B2 治本：token 硬上限闸门的检查节点。旧仅 merge/dispatch → analyze/plan/verify/handle_failure
# 等大量 LLM 调用可绕过。扩到所有主 LLM 节点边界，每个节点 end 都据【真实累计+估算】判超，
# 让 replan 空转/多轮 ReAct 的 runaway 成本在中途就被拦，而非跑到 merge 才发现。
_TOKEN_GATE_NODES = (
    "analyze", "plan", "elaborate", "dispatch", "merge",
    "verify_l2", "verify_l3", "handle_failure", "revision",
)


def _sync_task_from_state(task_id: str, state: dict[str, Any]) -> None:
    """将 Brain 状态片段写回 task_records"""
    updates: dict[str, Any] = {}

    complexity = state.get("complexity")
    if complexity is not None:
        updates["complexity"] = complexity.value if hasattr(complexity, "value") else str(complexity)

    plan = state.get("plan")
    if plan is not None:
        if hasattr(plan, "model_dump"):
            plan_dict = plan.model_dump(mode="json")
        elif isinstance(plan, dict):
            plan_dict = plan
        else:
            plan_dict = None
        if plan_dict is not None:
            updates["plan"] = plan_dict
            subtasks = plan_dict.get("subtasks") or []
            updates["subtask_count"] = len(subtasks)

    subtask_results = state.get("subtask_results")
    if isinstance(subtask_results, dict):
        # 治本(task 1bc867a1：concept 概览 completed 35 > count 34)：completed 不能用
        # len(subtask_results)——它累积了跨 replan/retry/rebase 的【全部】结果（含失败结果 +
        # st-N-2 重生成变体 + 已不在当前 plan 的旧 id），必然 > 当前 plan 的 subtask_count。
        # 正确语义 = 【在当前 plan 内 且 L1 通过】的子任务数；并夹紧到 subtask_count 兜底。
        from swarm.brain.nodes.shared import l1_passed as _passed

        plan_ids: set | None = None
        _plan_obj = state.get("plan")
        if _plan_obj is not None:
            _subs = getattr(_plan_obj, "subtasks", None)
            if _subs is None and isinstance(_plan_obj, dict):
                _subs = _plan_obj.get("subtasks")
            if _subs:
                plan_ids = {
                    (getattr(s, "id", None) if not isinstance(s, dict) else s.get("id"))
                    for s in _subs
                }
        if plan_ids:
            done = sum(1 for sid, out in subtask_results.items() if sid in plan_ids and _passed(out))
        else:
            done = sum(1 for out in subtask_results.values() if _passed(out))
        _cnt = updates.get("subtask_count")
        if isinstance(_cnt, int) and done > _cnt:
            done = _cnt  # 兜底夹紧：completed 永不超过 subtask_count
        updates["completed_subtasks"] = done

        # round18 P2 治本（三本账：完成/放弃/剩余）：进度只显 completed/count 会误导
        # "卡在 12/38"——实则放弃单元(重试耗尽 abandoned + 保 build 放弃 give_up)不计入,
        # 与 MONITOR 口径不一致(round18 教训#3:三盯要三本账)。补 abandoned 计数(限当前 plan)。
        _abandoned_ids = (
            set(state.get("abandoned_subtask_ids") or [])
            | set(state.get("give_up_isolated_ids") or [])
        )
        if plan_ids:
            _ab = sum(1 for sid in _abandoned_ids if sid in plan_ids)
        else:
            _ab = len(_abandoned_ids)
        # completed 与 abandoned 互斥(放弃者未 passed);夹紧使 completed+abandoned≤count,
        # 保证派生的 remaining=count-completed-abandoned 永非负。
        if isinstance(_cnt, int):
            _ab = max(0, min(_ab, _cnt - done))
        updates["abandoned_subtasks"] = _ab

    merged_diff = state.get("merged_diff")
    if merged_diff:
        updates["merged_diff"] = merged_diff

    merge_conflicts = state.get("merge_conflicts")
    if merge_conflicts:
        updates["merge_conflicts"] = merge_conflicts

    l3_fields: dict[str, Any] = {}
    for key in ("l3_passed", "l3_skipped", "l3_message"):
        if key in state:
            l3_fields[key] = state[key]
    if l3_fields:
        updates["l3_result"] = l3_fields

    human_decision = state.get("human_decision")
    if human_decision is not None:
        val = human_decision.value if hasattr(human_decision, "value") else str(human_decision)
        updates["human_decision"] = val.upper()

    if updates:
        try:
            store.update_task(task_id, **updates)
        except Exception as exc:
            logger.warning("[RUNNER] 同步任务状态失败 %s: %s", task_id, exc)


async def _stream_brain_events(
    task_id: str,
    graph_input: BrainState | Command,
    queue: asyncio.Queue[dict[str, Any]],
    *,
    project_id: str = "",
    module_lock: Any | None = None,
) -> tuple[dict[str, Any], Any, Any | None]:
    """执行 Brain 并流式推送节点事件，返回 (state values, snapshot)"""
    from swarm.tracing import brain_graph_config

    graph = get_compiled_brain_graph()
    task_rec = store.get_task(task_id) or {}
    thread_id = task_rec.get("thread_id") or task_id
    resume = isinstance(graph_input, Command)
    pid = project_id or task_rec.get("project_id") or ""
    # recursion_limit 按计划规模放大（RUN6：45 子任务 ultra 撞穿固定 50 崩）。复杂度/子任务数
    # 取自任务记录与已存计划——重跑/resume 时已有，新任务首轮按复杂度档兜底。
    _plan_rec = task_rec.get("plan")
    _subtask_n = None
    if isinstance(_plan_rec, dict):
        _subs = _plan_rec.get("subtasks")
        _subtask_n = len(_subs) if isinstance(_subs, list) else None
    config = brain_graph_config(
        task_id=task_id,
        project_id=pid,
        thread_id=thread_id,
        resume=resume,
        description=(task_rec.get("description") or "")[:200],
        complexity=task_rec.get("complexity"),
        subtask_count=_subtask_n,
    )
    progress = 10
    # P1-B：本次执行段墙钟起点 + 弹性预算。每个图事件循环顶检查；超【弹性】上限 → raise →
    # 归一化 FAILED + 释放资源。★弹性随规划揭示的子任务数动态放宽，绝不误杀合法大型任务★。
    _wc_cfg = get_config()
    _wc_base_s = _wc_cfg.task_deadline_s
    _wc_per_subtask_s = _wc_cfg.task_deadline_per_subtask_s
    _wc_subtasks = _subtask_n or 0  # 规划前多为 0（只用 base）；下方循环见 plan 输出后放宽
    _wc_start = time.monotonic()

    await _emit(queue, {
        "step": "brain_invoke",
        "status": "running",
        "message": "Brain 开始编排…",
        "mode": "brain",
        "progress": progress,
    })

    # B2：把本执行段内所有 LLM 用量归属到该 task（供 per-task 真实累计闸门）。
    # 单进程拓扑下 astream_events 及其派生的 worker 子任务共享本上下文，record() 据此归属。
    # 反注册/清理由 run_task 的 finally 统一做（覆盖正常/超限/锁丢/墙钟等所有退出路径）。
    from swarm.models import usage_tracker as _usage_tracker
    _usage_tracker.set_current_task(task_id)

    async for event in graph.astream_events(graph_input, config=config, version="v2"):
        # P1-B：弹性墙钟闸门——失控任务（replan 空转/卡节点）到点中止，防无上限占沙箱/GPU。
        # 有效上限按当前已知子任务数动态计算（规划揭示规模后自动放宽，不误杀大型任务）。
        _wc_effective = _effective_deadline_s(_wc_base_s, _wc_per_subtask_s, _wc_subtasks)
        try:
            _raise_if_wallclock_exceeded(_wc_start, _wc_effective)
        except TaskWallclockExceeded as _wc_exc:
            await _emit(queue, {
                "step": "wallclock_exceeded",
                "status": "failed",
                "message": str(_wc_exc) + f"（子任务 {_wc_subtasks}，弹性上限）；已中止并释放资源",
                "mode": "brain",
                "progress": 100,
            })
            raise
        # A-P1-14：搭车续期模块锁——build 跑得比 TTL 久时不至于静默失锁。
        # 无额外后台任务，进程处理每个图事件时顺带 renew（Redis 关闭时 no-op）。
        # ★对抗复核 2nd#2 治本★：renew 返回 False = 锁已在 Redis 侧丢失（另一任务可能已抢占同
        # 模块并发写工作树）→ 不能再继续，fail-fast 中止本任务（经 except Exception → FAILED +
        # finally 释放资源）。内存兜底/未启用 Redis 时 renew 恒 True，不触发（单进程无跨进程互斥意义）。
        if module_lock is not None and not module_lock.renew():
            await _emit(queue, {
                "step": "lock_lost", "status": "failed",
                "message": "模块锁已失效（TTL 超期/Redis 抖动），为防同模块并发写已中止任务",
                "mode": "brain", "progress": 100,
            })
            raise TaskLockLost(getattr(module_lock, "key", "?"))
        kind = event.get("event", "")
        if kind == "on_chain_start":
            name = event.get("name", "")
            if name and name not in ("LangGraph", "ChannelWrite", "increment_retry"):
                progress = min(progress + 4, 90)
                status = _NODE_STATUS_MAP.get(name)
                if status:
                    store.update_task(task_id, status=status)
                await _emit(queue, {
                    "step": "brain_node",
                    "status": "running",
                    "message": f"Brain 节点: {name}",
                    "mode": "brain",
                    "node": name,
                    "progress": progress,
                })
        elif kind == "on_chain_end":
            name = event.get("name", "")
            output = (event.get("data") or {}).get("output") or {}
            if name in _SYNC_ON_NODES and isinstance(output, dict):
                _sync_task_from_state(task_id, output)
                # P1-B：规划/拆分揭示子任务数 → 放宽弹性墙钟（只增不减，防大型任务被基线上限误杀）。
                _plan_out = output.get("plan")
                _subs = None
                if hasattr(_plan_out, "subtasks"):
                    _subs = getattr(_plan_out, "subtasks", None)
                elif isinstance(_plan_out, dict):
                    _subs = _plan_out.get("subtasks")
                if isinstance(_subs, list) and len(_subs) > _wc_subtasks:
                    _wc_subtasks = len(_subs)
            if name in _TOKEN_GATE_NODES and isinstance(output, dict):
                fresh = store.get_task(task_id) or task_rec
                ok, usage = store.check_task_token_limit(
                    task_id,
                    description=fresh.get("description") or "",
                    merged_diff=output.get("merged_diff") or fresh.get("merged_diff") or "",
                    subtask_results=output.get("subtask_results"),
                    # round27 弹性预算：复用墙钟维护的子任务数（规划揭示后放宽，与 P1-B 同理）
                    subtask_count=_wc_subtasks,
                )
                if not ok:
                    await _emit(queue, {
                        "step": "token_limit",
                        "status": "failed",
                        "message": (
                            f"单任务 token 超限 ({usage.get('total')}/"
                            f"{usage.get('limit_effective', get_config().max_task_tokens)}"
                            f"，弹性上限=base+per_subtask×{_wc_subtasks})"
                        ),
                        "mode": "brain",
                        "progress": 100,
                    })
                    raise TaskTokenLimitExceeded(usage)
            if name == "analyze":
                if isinstance(output, dict):
                    kc = output.get("knowledge_context") or {}
                    complexity = output.get("complexity")
                    if hasattr(complexity, "value"):
                        complexity = complexity.value
                    stats = {
                        "struct_count": len(kc.get("struct") or []),
                        "semantic_count": len(kc.get("semantic") or []),
                        "norms_count": len(kc.get("norms") or []),
                        "mistakes_count": len(kc.get("mistakes") or []),
                        "successes_count": len(kc.get("successes") or []),
                    }
                    await _emit(queue, {
                        "step": "knowledge_retrieved",
                        "status": "done",
                        "node": "analyze",
                        "complexity": str(complexity) if complexity else None,
                        "knowledge_stats": stats,
                        "message": (
                            f"知识检索: Harness {stats['norms_count']} · "
                            f"符号 {stats['struct_count']} · "
                            f"错题 {stats['mistakes_count']}"
                        ),
                        "mode": "brain",
                        "progress": progress,
                    })
                    await _emit(queue, {
                        "step": "brain_node",
                        "status": "done",
                        "node": "analyze",
                        "mode": "brain",
                        "progress": progress,
                    })
            if name == "plan" and module_lock is not None and isinstance(output, dict):
                plan_obj = output.get("plan")
                if plan_obj is not None:
                    from swarm.infra.redis_client import upgrade_module_lock

                    if hasattr(plan_obj, "model_dump"):
                        plan_dict = plan_obj.model_dump(mode="json")
                    elif isinstance(plan_obj, dict):
                        plan_dict = plan_obj
                    else:
                        plan_dict = None
                    if plan_dict is not None:
                        module_lock = upgrade_module_lock(module_lock, pid, plan_dict)

    snapshot = await graph.aget_state(config)
    final_state = dict(snapshot.values) if snapshot and snapshot.values else {}
    return final_state, snapshot, module_lock


def _extract_interrupt_info(snapshot: Any, state: dict[str, Any]) -> dict[str, Any] | None:
    """从 LangGraph snapshot 或 state 中提取 interrupt 载荷"""
    interrupts = getattr(snapshot, "interrupts", None) if snapshot is not None else None
    if interrupts:
        payload = interrupts[0]
        val = payload.value if hasattr(payload, "value") else payload
        if isinstance(val, dict):
            return val
        return {"type": str(val)}

    # 兼容 invoke 返回值
    legacy = state.get("__interrupt__")
    if legacy:
        if isinstance(legacy, list) and legacy:
            payload = legacy[0]
            val = payload.value if hasattr(payload, "value") else payload
            if isinstance(val, dict):
                return val
    return None


async def _has_pending_checkpoint(task_id: str) -> bool | None:
    """探测任务在 LangGraph checkpoint 里是否仍处于【挂起（有下一步/interrupt）】状态。

    对账用（2nd#1 + 复核 #6/M-1）：三态区分——
    - True：有快照且有 next/interrupt = 真挂起，可 resume。
    - False：快照【确】不存在(aget_state 干净返 None) = DB 假挂起，判 FAILED。
    - None：探测【本身失败】(PG 瞬时/持久故障) ≠ 无快照 → 调用方保守【保留但计数告警】，
      不批量误杀，也不静默永卡（持续失败由调用方汇总 loud 告警）。
    纯读、不推进图。
    """
    from swarm.tracing import brain_graph_config

    try:
        graph = get_compiled_brain_graph()
        task_rec = store.get_task(task_id) or {}
        thread_id = task_rec.get("thread_id") or task_id
        config = brain_graph_config(
            task_id=task_id,
            project_id=task_rec.get("project_id") or "",
            thread_id=thread_id,
            resume=False,
            description=(task_rec.get("description") or "")[:200],
            complexity=task_rec.get("complexity"),
            subtask_count=None,
        )
        snapshot = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001
        # ★B6 复核 #6/M-1★：探测【本身失败】(PG 瞬时抖动/持久故障) ≠ "无 checkpoint"。返 None（非
        # False）→ 对账保守【保留但计数】，既不批量误杀(旧 False 的坑)，也不静默永卡(旧改 True 的坑：
        # 持久故障下任务永远 kept 无告警)。持续失败由对账循环汇总 loud 告警,ops 可介入。
        logger.warning("[RECONCILE] checkpoint 探测失败(非「无快照」，保留待恢复) task=%s: %s", task_id, exc)
        return None
    if snapshot is None:
        return False
    # 有 next（待执行节点）或有 interrupt 载荷 → 仍是可续跑的挂起点。
    has_next = bool(getattr(snapshot, "next", None))
    state = dict(snapshot.values) if getattr(snapshot, "values", None) else {}
    has_interrupt = _extract_interrupt_info(snapshot, state) is not None
    return has_next or has_interrupt


async def get_pending_interrupt(task_id: str) -> dict[str, Any] | None:
    """读任务的 LangGraph 快照，返回当前【挂起的 interrupt】，供前端刷新后恢复人机交互卡片。

    治本(task 661ecacb)：澄清/审核卡片此前只由瞬时 SSE 事件渲染，刷新页面后无法找回 →
    挂起的澄清问题丢失、无法答复。本函数读取实时快照（纯读、不推进图），让前端在选中任务时
    主动拉取并重渲染。无挂起 / 非人机交互类型返回 None。
    """
    from swarm.tracing import brain_graph_config

    graph = get_compiled_brain_graph()
    task_rec = store.get_task(task_id) or {}
    thread_id = task_rec.get("thread_id") or task_id
    _plan_rec = task_rec.get("plan")
    _subtask_n = None
    if isinstance(_plan_rec, dict):
        _subs = _plan_rec.get("subtasks")
        _subtask_n = len(_subs) if isinstance(_subs, list) else None
    config = brain_graph_config(
        task_id=task_id,
        project_id=task_rec.get("project_id") or "",
        thread_id=thread_id,
        resume=False,
        description=(task_rec.get("description") or "")[:200],
        complexity=task_rec.get("complexity"),
        subtask_count=_subtask_n,
    )
    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001 — 读快照失败不应 500，返回无挂起
        logger.debug("[PENDING] 读取快照失败 task=%s: %s", task_id, exc)
        return None
    state = dict(snapshot.values) if snapshot and snapshot.values else {}
    info = _extract_interrupt_info(snapshot, state)
    if not info:
        return None
    itype = info.get("type", "")
    if itype not in _REVIEW_INTERRUPT_TYPES:
        return None
    _status, label = _INTERRUPT_STATUS_LABEL.get(itype, ("DELIVERING", "结果审核"))
    return {"interrupt_type": itype, "interrupt": info, "label": label}


async def _handle_post_run(
    task_id: str,
    state: dict[str, Any],
    queue: asyncio.Queue[dict[str, Any]],
    snapshot: Any = None,
) -> None:
    """运行结束后：同步 DB、判断是否在 interrupt 等待人工"""
    _sync_task_from_state(task_id, state)

    interrupt_info = _extract_interrupt_info(snapshot, state)
    if interrupt_info:
        interrupt_type = interrupt_info.get("type", "")
        if interrupt_type in _REVIEW_INTERRUPT_TYPES:
            status, label = _INTERRUPT_STATUS_LABEL.get(interrupt_type, ("DELIVERING", "结果审核"))
            store.update_task(task_id, status=status)
            await _emit(queue, {
                "step": "awaiting_review",
                "status": "waiting",
                "message": f"⏸ 等待人工{label}",
                "mode": "brain",
                "interrupt_type": interrupt_type,
                "interrupt": interrupt_info,
                "progress": 95,
            })
            return

    # P1-DEBT-07 根因修复（终态判定与图路由同源）：
    # human_decision 是图 after_confirm/after_deliver 路由到失败分支（END/LEARN_FAILURE）
    # 的【权威信号】，由 confirm/deliver 节点直接产出，一路保留到 END，不被后续节点污染。
    # 原 runner 只拦"plan_invalid"或"REJECT+confirm_reason"（仅 CONFIRM 来源），
    # 漏了 DELIVER 阶段的 REJECT（虚假前提阻断 / handle_failure escalate）——这些走
    # DELIVER→LEARN_FAILURE，confirm_reason 为空、verification_failure 也常为空，
    # 于是落到下方 gates 复核；而 gates 看的 l2_passed 在 BrainState last-write-wins
    # （P1-DEBT-06）下会被回填污染成 True → 误判可放行 → 假 DONE（task 69d34b1b 实证：
    # 走 LEARN_FAILURE、human_decision=REJECT、0 产出，却落 status=DONE）。
    # 修法：只要终态 human_decision==REJECT，一律判失败终态，与图路由严格同源。
    _hd = state.get("human_decision")
    _hd_val = _hd.value if hasattr(_hd, "value") else str(_hd or "")
    _vf = state.get("verification_failure")
    _is_reject = _hd_val == HumanDecision.REJECT.value
    if _vf == "plan_invalid" or _is_reject:
        issues = state.get("plan_validation_issues") or []
        # 归因优先级：plan 校验问题 > deliver 自动拒绝原因 > confirm 原因 > 兜底
        reason = (
            "; ".join(issues)
            or state.get("deliver_auto_reject_reason")
            or state.get("confirm_reason")
            or "任务未达成功终态，已 fail-fast 终止"
        )
        logger.warning("[RUNNER] 任务 %s REJECT/非法终态 fail-fast: %s", task_id, reason)
        _rec = store.get_task(task_id) or {}
        store.update_task(task_id, status="FAILED")
        _emit_task_notification(task_id, _rec, "FAILED")
        audit("task_failed", orchestrator="Brain", task_id=task_id,
              project_id=_rec.get("project_id"),
              error=f"rejected: {reason}"[:300])
        await _emit(queue, {
            "step": "error", "status": "error",
            "message": f"任务未达成功终态，已终止：{reason}",
            "mode": "brain", "progress": -1,
        })
        return

    # P1-DEBT-07 修复：终态判定下沉 gates 单一事实源。原 runner 只拦 plan_invalid/REJECT，
    # 漏了 failure_escalated / 未恢复失败子任务 / L2 未过 等——这些任务会走到下方"正常结束"
    # 被无脑标 DONE（learn_failure 已学错题却对外报成功 = 假 DONE）。auto_accept 模式下用
    # gates.can_auto_accept_delivery 复核：不可放行则标 FAILED。
    if state.get("auto_accept"):
        from swarm.brain import gates
        _allow, _reason = gates.can_auto_accept_delivery(state)
        if not _allow:
            logger.warning("[RUNNER] 任务 %s 终态非成功（gates 复核）: %s", task_id, _reason)
            _rec = store.get_task(task_id) or {}
            store.update_task(task_id, status="FAILED")
            _emit_task_notification(task_id, _rec, "FAILED")
            audit("task_failed", orchestrator="Brain", task_id=task_id,
                  project_id=_rec.get("project_id"),
                  error=f"delivery_not_accepted: {_reason}"[:300])
            await _emit(queue, {
                "step": "error", "status": "error",
                "message": f"任务未达成功终态：{_reason}",
                "mode": "brain", "progress": -1,
            })
            return

    # 正常结束
    task_rec = store.get_task(task_id) or {}
    token_usage = store.estimate_token_usage(
        description=task_rec.get("description") or state.get("task_description") or "",
        merged_diff=state.get("merged_diff") or "",
        subtask_results=state.get("subtask_results"),
    )
    duration = store.compute_task_duration_seconds(task_rec)
    # 部分交付：有子任务被放弃(重试耗尽)或保 build 放弃(阶梯三 revert/桩)→ 终态 PARTIAL(非 DONE)。
    # 已完成子任务的真实产物照常落盘/合并/过 L2，但任务【诚实标未完成】，列明放弃/桩项——绝不当
    # DONE 假成功。give_up_isolated_ids 是阶梯三保 build 放弃的子任务（本地树已清/打桩，build 未毒），
    # 与 abandoned（重试耗尽连坐放弃）合并判 PARTIAL。
    from swarm.brain.gates import partial_delivery_ids
    _abandoned = state.get("abandoned_subtask_ids") or []
    _given_up = state.get("give_up_isolated_ids") or []
    _rebase_dropped = state.get("merge_rebase_dropped") or []  # 复核 H-1：rebase 超限丢弃的子任务
    _partial_ids = partial_delivery_ids(state)  # 单一事实源：abandoned ∪ give_up ∪ rebase_dropped
    _final_status = "PARTIAL" if _partial_ids else "DONE"
    store.update_task(
        task_id,
        status=_final_status,
        token_usage=token_usage,
        duration_seconds=round(duration, 2) if duration is not None else None,
    )
    _emit_task_notification(task_id, task_rec, _final_status)
    output_parts = _build_result_payload(state)
    if _partial_ids:
        logger.warning("[RUNNER] 任务 %s 部分交付(PARTIAL)：放弃 %d 个(重试耗尽 %s) + 保 build 放弃 %d 个(阶梯三 %s)"
                       " + rebase 超限丢弃 %d 个(%s)",
                       task_id, len(_abandoned), _abandoned, len(_given_up), _given_up,
                       len(_rebase_dropped), _rebase_dropped)
        _msg = (f"部分交付：已完成子任务真实落盘且可构建(已过 L2)；放弃 {len(_abandoned)} 个(重试耗尽)：{_abandoned}"
                if _abandoned else "部分交付：已完成子任务真实落盘且可构建(已过 L2)")
        if _given_up:
            _msg += f"；保 build 放弃 {len(_given_up)} 个(本地树已清/打桩，需人工补完)：{_given_up}"
        if _rebase_dropped:
            # 复核 H-1：否则 rebase-only PARTIAL 会显示"放弃 0 + 保 build 0"无解释。
            _msg += f"；merge rebase 超限丢弃 {len(_rebase_dropped)} 个(rebased 变更未并入，需人工核验)：{_rebase_dropped}"
        await _emit(queue, {
            "step": "complete",
            "status": "partial",
            "message": _msg,
            "mode": "brain",
            "progress": 100,
        })
    else:
        await _emit(queue, {
            "step": "complete",
            "status": "done",
            "message": "任务执行完成",
            "mode": "brain",
            "progress": 100,
        })
    await _emit(queue, {"step": "result", "mode": "brain", "result": output_parts})


def _build_result_payload(state: dict[str, Any]) -> dict[str, Any]:
    output_parts: dict[str, Any] = {}
    for key in ("merged_diff", "l2_passed", "learn_summary", "complexity", "plan", "subtask_results", "human_decision", "learned", "knowledge_context", "merge_conflicts", "l3_passed", "l3_skipped", "l3_message", "plan_validation_issues", "shared_contract", "verification_failure"):
        val = state.get(key)
        if val is None or val == "" or val == {}:
            continue
        if hasattr(val, "model_dump"):
            output_parts[key] = val.model_dump(mode="json")
        elif isinstance(val, dict):
            output_parts[key] = val
        else:
            output_parts[key] = str(val) if not isinstance(val, (bool, int, float)) else val
    return output_parts


async def run_task(
    task_id: str,
    project_id: str,
    description: str,
    auto_accept: bool | None = None,
) -> None:
    """后台启动 Brain 任务（从 SUBMITTED 到 DONE 或 interrupt）"""
    # 绑定 task 上下文：本协程（asyncio.Task）内所有 swarm 日志自动带 [task=...]。
    # 放在 run_task 内可覆盖所有入口（调度器准入 / 后台 / 直接调用）。
    from swarm.logging_config import set_task_context

    set_task_context(task_id, project_id=project_id or "")  # P2-D：日志带 project_id
    # 绑定 project 上下文：本协程内所有 brain 编排 LLM 调用（ANALYZE/TECH_DESIGN/plan/
    # dispatch/HANDLE_FAILURE 等均 await llm.ainvoke）经 ContextVar 自动归属本项目，供
    # token 用量统计按项目聚合。brain 全异步→ContextVar 沿 await 链 + gather 子任务(创建时
    # copy_context)自然传播；每个 run_task 是独立 asyncio.Task，互不串项目。worker 侧另在
    # executor 派发前各自设置（可能跨进程/线程，须显式）。
    from swarm.knowledge.service import set_worker_context

    set_worker_context(project_id or None)
    queue = register_task_queue(task_id)
    # 复核 R23-6：check→add 之间【无 await】——单进程 asyncio 无抢占，此认领在目标拓扑(单 brain
    # 进程)下即原子，同 task 不会双跑。多副本部署需跨进程认领(Redis/DB claim，scheduler.is_task_claimed
    # 为其入口)，属已知架构边界。改动此块务必保持 check 与 add 之间不引入 await，否则重新出现 TOCTOU。
    if task_id in _task_running:
        await _emit(queue, {"step": "error", "status": "error", "message": "任务已在执行中"})
        return

    _task_running.add(task_id)
    _set_workspace(project_id)

    from swarm.infra.redis_client import ModuleLock, TaskQueue

    TaskQueue.enqueue(task_id, project_id)
    module_lock = ModuleLock(project_id, "default")
    if not module_lock.acquire():
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": "同项目模块锁被占用，请稍后重试",
        })
        _task_running.discard(task_id)
        return

    # round27 F1：锁获取到旧 try 之间的初始化段（store.get_task / load_profile_prompts /
    # build_session_metadata 等均可抛）原先【不在】下方 try/finally 保护内——异常直接泄漏模块锁
    # （Redis 路径靠 1h TTL 自愈；Redis 不可用退进程内 threading 锁的兜底路径【永久】泄漏到重启）
    # + _task_running 残留。try 前移到紧贴 acquire，让一切失败路径统一 FAILED 落库 + finally 释放。
    try:
        if auto_accept is None:
            auto_accept = os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")

        task_rec = store.get_task(task_id) or {}
        user_id = task_rec.get("created_by_user_id") or ""
        from swarm.memory.profile import load_profile_prompts

        profile, brain_prompt, worker_prompt = load_profile_prompts(
            user_id or None,
            project_id,
        )

        initial_state: BrainState = {
            "task_id": task_id,
            "task_description": description,
            "project_id": project_id,
            "user_id": user_id,
            "user_profile": profile,
            "user_profile_prompt_brain": brain_prompt,
            "user_profile_prompt_worker": worker_prompt,
            "auto_accept": auto_accept,
        }

        # B 部分：透传上传文件给摄取节点（存在 task_records.uploaded_files）。
        # 无文件时 ingest 节点 no-op 直通，对纯文字任务零影响。
        _uploaded = task_rec.get("uploaded_files") or []
        if _uploaded:
            initial_state["uploaded_files"] = list(_uploaded)
        if task_rec.get("auto_confirm_vision"):
            initial_state["auto_confirm_vision"] = True

        project_path = None
        try:
            proj = store.get_project(project_id)
            if proj:
                project_path = proj.get("path")
        except Exception as exc:
            logger.debug("获取项目路径失败 project_id=%s: %s", project_id, exc)

        # ── 3rd#2 治本：任务启动即钉住 base commit（git rev-parse HEAD）──
        # 全交付链（worker diff / merge base / L2 reset / learn 复位）统一相对此 SHA，
        # 消除运行期用户/兄弟任务 commit 推进 HEAD 造成的混基线。已钉扎（罕见的重跑同 task_id）
        # 则沿用 DB 里的出生基线，绝不重捕获。非 git/greenfield → None → 各站点退回 HEAD（零回归）。
        from swarm.git_base import capture_base_commit

        base_commit = task_rec.get("base_commit") or capture_base_commit(project_path)
        if base_commit:
            initial_state["base_commit"] = base_commit
            if not task_rec.get("base_commit"):
                try:
                    store.update_task(task_id, base_commit=base_commit)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[BASE] 落库 base_commit 失败(非致命,内存态仍钉扎): %s", exc)
        elif project_path and os.path.isdir(os.path.join(project_path, ".git")):
            # 对抗复核 M1：git 项目却捕获不到 base（git 超时/不在 PATH/仓库损坏）→ 全链退回实时 HEAD，
            # 运行期 HEAD 漂移不再受钉扎保护。必须【可观测】——否则运维看到交付异常却无线索。
            logger.warning(
                "[BASE] capture_base_commit 返回 None（git 不可用/仓库异常），任务 %s 回退 HEAD 行为，"
                "运行期 HEAD 漂移不受钉扎保护: project_path=%s", task_id, project_path,
            )

        from swarm.memory.session import build_session_metadata

        initial_state["session_metadata"] = build_session_metadata(
            project_path=project_path,
            client="api",
        )

        thread_id = task_rec.get("thread_id") or task_id
        store.update_task(task_id, status="ANALYZING", thread_id=thread_id)
        audit(
            "task_start",
            orchestrator="Brain",
            task_id=task_id,
            project_id=project_id,
            description=description[:200],
        )
        state, snapshot, module_lock = await _stream_brain_events(
            task_id, initial_state, queue, project_id=project_id, module_lock=module_lock,
        )
        await _handle_post_run(task_id, state, queue, snapshot)
        _final_rec = store.get_task(task_id)
        audit(
            "task_complete",
            orchestrator="Brain",
            task_id=task_id,
            project_id=project_id,
            status=_final_rec.get("status") if _final_rec else "UNKNOWN",
        )
    except asyncio.CancelledError:
        logger.info("[RUNNER] 任务 %s 已取消", task_id)
        store.update_task(task_id, status="CANCELLED")
        audit("task_cancelled", orchestrator="Brain", task_id=task_id, project_id=project_id)
        await _emit(queue, {
            "step": "cancelled",
            "status": "cancelled",
            "message": "任务已取消",
            "progress": -1,
        })
    except Exception as exc:
        logger.exception("[RUNNER] 任务 %s 执行失败", task_id)
        store.update_task(task_id, status="FAILED")
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
        audit("task_failed", orchestrator="Brain", task_id=task_id, project_id=project_id, error=str(exc)[:300])
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": f"执行失败: {exc}",
            "progress": -1,
        })
    finally:
        module_lock.release()
        _task_running.discard(task_id)
        # B2：清理 per-task token 归属与真实累计（覆盖正常/超限/异常所有退出路径）。
        try:
            from swarm.models import usage_tracker as _ut
            _ut.set_current_task(None)
            _ut.clear_task_total(task_id)
        except Exception:
            pass
        # 兜底：释放本任务残留的沙箱（正常路径 worker 已自清，此处防漏）
        try:
            from swarm.worker.sandbox import get_sandbox_manager

            get_sandbox_manager().kill_by_task(task_id)
        except Exception:
            pass


async def resume_task(
    task_id: str,
    decision: str,
    feedback: str = "",
    revert_status: str | None = None,
) -> None:
    """恢复被 interrupt 暂停的任务。

    revert_status（P1-A）：端点在原子认领时已把状态推出人工闸态（→ ANALYZING/IN_REVISION）；
    若此处因【同项目模块锁被占用】这类瞬时原因未能真正开跑，须把状态【回滚到原审核态】，
    否则任务卡在 ANALYZING 却无 resume 在跑、且用户无法再次点通过（认领 gate 已关）。
    """
    from swarm.logging_config import set_task_context

    set_task_context(task_id)
    queue = _task_queues.get(task_id) or register_task_queue(task_id)

    if task_id in _task_running:
        # 对抗复核 #2：认领已把状态推出人工闸态，此处并发早退须回滚，否则任务卡 ANALYZING/
        # IN_REVISION 且用户无法再点审批（认领 gate 已关），只能等重启对账。
        if revert_status:
            store.update_task(task_id, status=revert_status)
        await _emit(queue, {"step": "error", "status": "error", "message": "任务正在执行，请稍候"})
        return

    task = store.get_task(task_id)
    if not task:
        await _emit(queue, {"step": "error", "status": "error", "message": "任务不存在"})
        return

    _task_running.add(task_id)
    _set_workspace(task["project_id"])

    # 与 run_task 一致：resume 也要持同项目模块锁，否则两个 resume / resume+run_task
    # 并发改同一项目工作树会互相踩（无串行化）。
    from swarm.infra.redis_client import ModuleLock, TaskQueue

    _resume_project_id = task.get("project_id", "")
    if _resume_project_id:
        set_task_context(task_id, project_id=_resume_project_id)  # 复核 F6：resume 日志也带 project_id
    TaskQueue.enqueue(task_id, _resume_project_id)
    module_lock = ModuleLock(_resume_project_id, "default")
    if not module_lock.acquire():
        # 瞬时锁占用 → 回滚认领状态，让用户可重试（否则卡 ANALYZING 无 resume）。
        if revert_status:
            store.update_task(task_id, status=revert_status)
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": "同项目模块锁被占用，请稍后重试",
        })
        _task_running.discard(task_id)
        return

    # round27 F1：与 run_task 同理——acquire 到旧 try 之间的 store.update_task 可抛，
    # 异常会泄漏模块锁（进程内锁兜底路径永久泄漏）。try 前移到紧贴 acquire。
    try:
        decision_norm = decision.lower().strip()
        if decision_norm in ("approved", "approve", "accept"):
            decision_norm = HumanDecision.ACCEPT.value
        elif decision_norm in ("revised", "revise"):
            decision_norm = HumanDecision.REVISE.value
        elif decision_norm in ("rejected", "reject"):
            decision_norm = HumanDecision.REJECT.value

        store.update_task(
            task_id,
            human_decision=decision_norm.upper(),
            status="IN_REVISION" if decision_norm == HumanDecision.REVISE.value else "ANALYZING",
        )

        resume_payload: dict[str, Any] = {"decision": decision_norm, "feedback": feedback}

        await _emit(queue, {
            "step": "resume",
            "status": "running",
            "message": f"恢复执行: {decision_norm}",
            "mode": "brain",
            "progress": 50,
        })
        state, snapshot, module_lock = await _stream_brain_events(
            task_id,
            Command(resume=resume_payload),
            queue,
            project_id=_resume_project_id,
            module_lock=module_lock,
        )
        await _handle_post_run(task_id, state, queue, snapshot)
    except asyncio.CancelledError:
        # F3：取消是 BaseException，不被 except Exception 捕获——须显式落 CANCELLED，
        # 否则 resume 途中被 cancel_task 取消会把任务卡在 ANALYZING/IN_REVISION（认领已推进的态）
        # 直到重启对账才转终态；与 run_task 的取消处理对齐。
        logger.info("[RUNNER] 任务 %s resume 已取消", task_id)
        store.update_task(task_id, status="CANCELLED")
        await _emit(queue, {
            "step": "cancelled", "status": "cancelled", "message": "任务已取消", "progress": -1,
        })
        raise
    except Exception as exc:
        logger.exception("[RUNNER] 任务 %s resume 失败", task_id)
        store.update_task(task_id, status="FAILED")
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": f"恢复失败: {exc}",
            "progress": -1,
        })
    finally:
        module_lock.release()
        _task_running.discard(task_id)
        # 复核 CR-1：resume 也经 _stream_brain_events→set_current_task，必须同样清理 per-task
        # token 归属+累计（否则 resume 后计数残留、retry 时被 max(真实,估算) 误判超限 + 内存泄漏）。
        try:
            from swarm.models import usage_tracker as _ut
            _ut.set_current_task(None)
            _ut.clear_task_total(task_id)
        except Exception:
            pass
        # P1-B：兜底释放本任务沙箱（如 revise-resume 再派发过 worker）——墙钟/异常中止时不泄漏。
        try:
            from swarm.worker.sandbox import get_sandbox_manager
            get_sandbox_manager().kill_by_task(task_id)
        except Exception:  # noqa: BLE001
            pass


async def resume_planning(
    task_id: str,
    payload: dict[str, Any],
    revert_status: str | None = None,
) -> None:
    """恢复被规划子图 interrupt（clarify / review_design）暂停的任务。

    与 resume_task 区别：clarify/review 的 resume 是结构化 payload（透传原样给 graph），
    不走 ACCEPT/REVISE/REJECT 那套人工决策归一化。
    - clarify：payload = {q_index: answer, ...} 或 {"action": "skip"}
    - review_design：payload = {"decision": "approve"|"reject", "feedback": "..."}
    """
    from swarm.logging_config import set_task_context

    set_task_context(task_id)

    queue = _task_queues.get(task_id) or register_task_queue(task_id)
    if task_id in _task_running:
        # 对抗复核 #2：与 resume_task 对齐——并发早退回滚认领态，防卡 ANALYZING 无法再审批。
        if revert_status:
            store.update_task(task_id, status=revert_status)
        await _emit(queue, {"step": "error", "status": "error", "message": "任务正在执行，请稍候"})
        return
    task = store.get_task(task_id)
    if not task:
        await _emit(queue, {"step": "error", "status": "error", "message": "任务不存在"})
        return

    _task_running.add(task_id)
    _set_workspace(task["project_id"])

    # 与 run_task / resume_task 一致：持同项目模块锁串行化工作树访问。
    from swarm.infra.redis_client import ModuleLock, TaskQueue

    _resume_project_id = task.get("project_id", "")
    if _resume_project_id:
        set_task_context(task_id, project_id=_resume_project_id)  # 复核 F6：resume 日志也带 project_id
    TaskQueue.enqueue(task_id, _resume_project_id)
    module_lock = ModuleLock(_resume_project_id, "default")
    if not module_lock.acquire():
        # 瞬时锁占用 → 回滚认领状态（回到 CLARIFYING/DESIGN_REVIEW），让用户可重试。
        if revert_status:
            store.update_task(task_id, status=revert_status)
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": "同项目模块锁被占用，请稍后重试",
        })
        _task_running.discard(task_id)
        return

    # round27 F1：与 run_task/resume_task 同族——update_task 移入 try，防 acquire 后异常泄漏模块锁。
    try:
        store.update_task(task_id, status="ANALYZING")
        await _emit(queue, {
            "step": "resume", "status": "running",
            "message": "恢复规划（澄清/方案评审已提交）", "mode": "brain", "progress": 30,
        })
        state, snapshot, module_lock = await _stream_brain_events(
            task_id,
            Command(resume=payload),
            queue,
            project_id=_resume_project_id,
            module_lock=module_lock,
        )
        await _handle_post_run(task_id, state, queue, snapshot)
    except asyncio.CancelledError:
        # F3：取消是 BaseException，须显式落 CANCELLED，否则规划 resume 途中被取消会卡在
        # ANALYZING 直到重启对账；与 run_task/resume_task 对齐。
        logger.info("[RUNNER] 任务 %s 规划 resume 已取消", task_id)
        store.update_task(task_id, status="CANCELLED")
        await _emit(queue, {
            "step": "cancelled", "status": "cancelled", "message": "任务已取消", "progress": -1,
        })
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[RUNNER] 任务 %s 规划 resume 失败", task_id)
        store.update_task(task_id, status="FAILED")
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
        await _emit(queue, {"step": "error", "status": "error", "message": f"规划恢复失败: {exc}", "progress": -1})
    finally:
        module_lock.release()
        _task_running.discard(task_id)
        # 复核 CR-1：规划 resume 同样 set_current_task，需清理 per-task token 归属+累计。
        try:
            from swarm.models import usage_tracker as _ut
            _ut.set_current_task(None)
            _ut.clear_task_total(task_id)
        except Exception:
            pass
        # P1-B：兜底释放本任务沙箱（规划恢复若再派发过 worker）——墙钟/异常中止时不泄漏。
        try:
            from swarm.worker.sandbox import get_sandbox_manager
            get_sandbox_manager().kill_by_task(task_id)
        except Exception:  # noqa: BLE001
            pass


def resume_planning_background(
    task_id: str, payload: dict[str, Any], revert_status: str | None = None
) -> None:
    """在 FastAPI 后台 resume 规划 interrupt。"""
    async def _wrap() -> None:
        try:
            from swarm.logging_config import bind_task
            with bind_task(task_id):
                await resume_planning(task_id, payload, revert_status=revert_status)
        except Exception:  # noqa: BLE001
            logger.exception("[RUNNER] resume_planning_background 失败 task=%s", task_id)
        finally:
            # 对抗复核：与 resume_task_background/start_task_background 对齐——清句柄，
            # 否则 cancel_task 可能 cancel 到过期句柄 + _task_handles 泄漏。
            _task_handles.pop(task_id, None)
    _task_handles[task_id] = asyncio.create_task(_wrap())


def is_task_running(task_id: str) -> bool:
    return task_id in _task_running


def is_task_orphaned(task_id: str) -> bool:
    """DB 为活跃状态但本进程未在跑（常见于 API 重启后）"""
    task = store.get_task(task_id)
    if not task:
        return False
    status = task.get("status", "")
    return status in _ACTIVE_DB_STATUSES and task_id not in _task_running


async def reconcile_orphan_tasks() -> dict[str, int]:
    """P0-A 启动对账：把全库"进行中"任务按态类别分治恢复/失败（统一崩溃恢复协议）。

    核心洞察：PG checkpoint 只存图状态、不存外部副作用态（沙箱/工作树/锁）。故不能一刀切
    "有 checkpoint 就 resume"，必须按态类别分治：
    - 中断挂起态（CONFIRMING/DELIVERING/CLARIFYING/DESIGN_REVIEW）：无在飞外部工作，
      checkpoint 是安全续跑点 → 保留，等人工闸经 Command(resume) 继续（依赖 PG checkpointer）。
    - SUBMITTED：已入队、尚未创建任何外部资源 → 重新入队自动恢复（无需 fail-closed）。
    - 其余活跃执行态（ANALYZING…LEARNING_*）：外部资源已死、无中途续跑入口 →
      fail-closed 标 FAILED(orphaned_on_restart) + 显式释放沙箱。

    幂等：本进程在跑的任务（_task_running）跳过。沙箱通常已由 on_startup 的
    _sweep_startup_orphans（按实例标签重扫服务端）清掉，此处 kill_by_task 为显式兜底。
    """
    loop = asyncio.get_running_loop()
    stats = {"resumed_interrupt": 0, "requeued": 0, "failed": 0, "skipped_running": 0}
    try:
        candidates = await loop.run_in_executor(None, store.list_orphan_candidates)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RECONCILE] 读孤儿候选失败，跳过启动对账: %s", exc)
        return stats

    from swarm.brain.scheduler import is_task_claimed

    for rec in candidates:
        tid = rec.get("id")
        status = rec.get("status", "")
        if not tid:
            continue
        # 本进程已认领（在跑 or 已出队进并发槽）→ 非孤儿，跳过（含调度器刚出队 Redis 残留项的窗口）。
        if is_task_claimed(tid):
            stats["skipped_running"] += 1
            continue

        if status in _INTERRUPT_SUSPENDED_STATES:
            # ★对抗复核 2nd#1 治本★：保留中断态前【探测 checkpoint 是否真的还在挂起点】。
            # 若 checkpoint 丢失（MemorySaver 重启 / 表损坏 / 只含 task_records 的备份恢复）→
            # DB 显示"待审"但 Command(resume) 无 snapshot 可续 → 用户点审批必 FAILED、上下文作废，
            # 是"假挂起"权威分裂。探测无挂起 → fail-closed 标 FAILED(checkpoint_missing)，让用户重提交。
            has_ckpt = await _has_pending_checkpoint(tid)
            if has_ckpt is None:
                # ★复核 M-1★：探测失败(非"确无快照")→ 保守保留 + 计数,不误杀也不静默永卡。
                stats["probe_failed"] = stats.get("probe_failed", 0) + 1
                _audit_reconcile(tid, rec, "checkpoint_probe_failed", status,
                                 "checkpointer probe failed; kept un-verified, verify manually")
                logger.warning("[RECONCILE] 任务 %s 中断态 %s checkpoint 探测失败 → 保留待恢复(未核验)", tid, status)
            elif has_ckpt:
                stats["resumed_interrupt"] += 1
                _audit_reconcile(tid, rec, "recovered_interrupt", status,
                                 "API restart; interrupt-suspended task kept for human resume")
                logger.info("[RECONCILE] 任务 %s 中断挂起态 %s 保留待人工 resume", tid, status)
            else:
                await loop.run_in_executor(
                    None, lambda t=tid: store.update_task(t, status="FAILED")
                )
                _audit_reconcile(tid, rec, "checkpoint_missing", "FAILED",
                                 "interrupt-suspended in DB but no checkpoint snapshot; cannot resume")
                stats["failed"] += 1
                logger.warning("[RECONCILE] 任务 %s 中断态 %s 但 checkpoint 丢失 → FAILED（假挂起，请重提交）",
                               tid, status)

        elif status == "SUBMITTED":
            try:
                from swarm.brain.scheduler import submit_task

                submit_task(
                    tid, rec["project_id"], rec["description"],
                    auto_accept=bool(rec.get("auto_accept", False)),
                    priority=rec.get("queue_priority") or "normal",
                )
                stats["requeued"] += 1
                logger.info("[RECONCILE] 任务 %s SUBMITTED 重入队自动恢复", tid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[RECONCILE] 任务 %s 重入队失败: %s", tid, exc)

        else:
            # 活跃执行态 fail-closed
            try:
                await loop.run_in_executor(
                    None, lambda t=tid: store.update_task(t, status="FAILED")
                )
                _audit_reconcile(tid, rec, "orphaned_on_restart", "FAILED",
                                 "API restart; active-execution task failed-closed, resources released")
                _task_running.discard(tid)
                try:
                    from swarm.worker.sandbox import get_sandbox_manager

                    get_sandbox_manager().kill_by_task(tid)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[RECONCILE] 任务 %s 释放沙箱兜底失败: %s", tid, exc)
                stats["failed"] += 1
                logger.info("[RECONCILE] 任务 %s 活跃执行态 %s → FAILED(orphaned_on_restart)", tid, status)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[RECONCILE] 任务 %s 标记 FAILED 失败: %s", tid, exc)

    if stats.get("probe_failed"):
        # ★复核 M-1★：持久探测失败不能静默——汇总 loud 告警，ops 据此排查 checkpointer 健康。
        logger.warning("[RECONCILE] ⚠️ %d 个中断态任务因 checkpoint 探测失败被【保留未核验】，"
                       "checkpointer 可能不健康，请人工排查并按需处置", stats["probe_failed"])
    logger.info("[RECONCILE] 启动对账完成: %s", stats)
    return stats


def _audit_reconcile(task_id: str, rec: dict[str, Any], event: str, status: str, detail: str) -> None:
    """对账留痕（append-only 审计），失败不阻断。"""
    try:
        store.append_task_audit(
            task_id, event=event, project_id=rec.get("project_id"),
            status=status, description=(rec.get("description") or "")[:200], detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[RECONCILE] 审计留痕失败 task=%s: %s", task_id, exc)


def can_retry_task(task_id: str) -> tuple[bool, str]:
    """是否允许重跑任务"""
    task = store.get_task(task_id)
    if not task:
        return False, "任务不存在"

    if task_id in _task_running:
        return False, "任务正在执行中"

    status = task.get("status", "")

    # 人工审核态优先拦截：即使本进程未在跑（orphaned），这类任务也需要
    # 先由人工 通过/修订/拒绝/答复 决策，而不是直接重跑（否则丢失待审产出/中断上下文）。
    # P0-D：对称覆盖全部中断挂起态（补 CLARIFYING/DESIGN_REVIEW，与 cancel/resume 口径一致）。
    if status in _INTERRUPT_SUSPENDED_STATES:
        return False, "任务等待人工审核，请先通过/修订/拒绝/答复"

    if is_task_orphaned(task_id):
        return True, ""

    # PARTIAL（部分交付：部分子任务放弃/保 build 桩）也允许重跑——
    # 否则一旦进入部分交付终态就永久卡死，无法对放弃的子任务再尝试。
    if status in _TERMINAL_STATES:
        return True, ""

    if status in _ACTIVE_DB_STATUSES:
        return False, "任务仍在执行中"

    return False, f"当前状态 {status} 不可重跑"


async def cancel_task(task_id: str) -> bool:
    """取消正在运行的任务，或将 orphaned 活跃任务标记为 CANCELLED。

    即使 DB 记录已不存在（如项目被删），仍必须取消内存中的 asyncio 句柄 +
    释放沙箱——否则 asyncio 任务会变成幽灵，陷入 replan 死循环持续烧 GPU。
    """
    task = store.get_task(task_id)

    handle = _task_handles.get(task_id)
    handle_cancelled = False
    if handle and not handle.done():
        handle.cancel()
        handle_cancelled = True
        try:
            await handle
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — 句柄内部异常不应阻断清理
            pass

    _task_running.discard(task_id)

    # 释放该任务占用的沙箱（释放远程小模型/容器资源）——取消时容器不会自动销毁。
    # CancelledError 不保证传播到 worker 的 finally（取消时机可能不在 await 点，
    # 或 brain 级 L2/L3 sandbox 不在 worker 生命周期内），故在此显式按 task 清理。
    try:
        from swarm.worker.sandbox import get_sandbox_manager

        killed = get_sandbox_manager().kill_by_task(task_id)
        if killed:
            logger.info("[RUNNER] 取消任务 %s 释放 %d 个沙箱", task_id, killed)
    except Exception as exc:
        logger.warning("[RUNNER] 取消任务 %s 释放沙箱失败: %s", task_id, exc)

    queue = _task_queues.get(task_id)
    if queue:
        await _emit(queue, {
            "step": "cancelled",
            "status": "cancelled",
            "message": "任务已取消",
            "progress": -1,
        })

    # DB 记录已被删（如级联删项目）→ 仅完成了内存侧清理，仍算成功取消。
    if task is None:
        if handle_cancelled:
            logger.info("[RUNNER] 任务 %s 无 DB 记录(可能项目已删)，已终止内存句柄+沙箱", task_id)
        return handle_cancelled

    if task.get("status") != "CANCELLED":
        store.update_task(task_id, status="CANCELLED")
    return True


async def cancel_project_tasks(project_id: str) -> int:
    """取消某项目下所有运行中的任务（删项目前调用，防止幽灵任务残留）。

    覆盖两类：(1) 内存中有 asyncio 句柄的活跃任务；(2) DB 标记为活跃状态的任务。
    返回取消的任务数。
    """
    cancelled = 0
    # 1) 内存中所有句柄属于该项目的（句柄字典只有 task_id，需查 DB 反查 project）
    candidate_ids: set[str] = set()
    for tid, handle in list(_task_handles.items()):
        if handle and not handle.done():
            candidate_ids.add(tid)
    # 2) DB 中该项目活跃状态的任务
    try:
        for t in store.list_tasks(project_id):
            if t.get("status") in _ACTIVE_DB_STATUSES:
                candidate_ids.add(t.get("id"))
    except Exception as exc:
        logger.warning("[RUNNER] 枚举项目 %s 活跃任务失败: %s", project_id, exc)

    # 对候选逐个取消（cancel_task 已能处理 DB 记录缺失的情况）
    for tid in candidate_ids:
        # 仅取消属于该项目的：若 DB 还能查到则校验 project_id，查不到则按内存句柄取消
        t = store.get_task(tid)
        if t is not None and t.get("project_id") != project_id:
            continue
        try:
            if await cancel_task(tid):
                cancelled += 1
        except Exception as exc:
            logger.warning("[RUNNER] 级联取消任务 %s 失败: %s", tid, exc)
    if cancelled:
        logger.info("[RUNNER] 项目 %s 级联取消 %d 个运行中任务", project_id, cancelled)
    return cancelled


async def retry_task(task_id: str, auto_accept: bool | None = None) -> bool:
    """重置任务字段并重新执行"""
    allowed, reason = can_retry_task(task_id)
    if not allowed:
        logger.warning("[RUNNER] 任务 %s 不可重跑: %s", task_id, reason)
        return False

    task = store.get_task(task_id)
    if not task:
        return False

    if task_id in _task_running:
        await cancel_task(task_id)

    new_thread_id = f"{task_id}-r-{secrets.token_hex(4)}"
    store.update_task(
        task_id,
        status="SUBMITTED",
        plan={},
        merged_diff="",
        subtask_count=0,
        completed_subtasks=0,
        human_decision="",
        thread_id=new_thread_id,
        base_commit="",  # ★B6 复核 #5★：retry=全新 thread/清空 plan → 清 base_commit 令 run_task
                         # 重捕获【当前仓库 HEAD】为新基线（retry 语义=对最新仓库重跑，非沿用旧 birth base）。
    )

    await run_task(
        task_id,
        task["project_id"],
        task["description"],
        auto_accept=auto_accept,
    )
    return True


def start_task_background(
    task_id: str,
    project_id: str,
    description: str,
    auto_accept: bool = False,
) -> None:
    """在 FastAPI 后台启动任务（非阻塞）"""
    register_task_queue(task_id)

    async def _wrap() -> None:
        from swarm.logging_config import bind_task

        with bind_task(task_id):
            try:
                await run_task(task_id, project_id, description, auto_accept=auto_accept)
            finally:
                _task_handles.pop(task_id, None)

    _task_handles[task_id] = asyncio.create_task(_wrap())


def resume_task_background(
    task_id: str, decision: str, feedback: str = "", revert_status: str | None = None
) -> None:
    """在 FastAPI 后台 resume 任务"""
    async def _wrap() -> None:
        from swarm.logging_config import bind_task

        with bind_task(task_id):
            try:
                await resume_task(task_id, decision, feedback, revert_status=revert_status)
            finally:
                _task_handles.pop(task_id, None)

    _task_handles[task_id] = asyncio.create_task(_wrap())


def cancel_task_background(task_id: str) -> None:
    """在 FastAPI 后台取消任务"""
    asyncio.create_task(cancel_task(task_id))


def retry_task_background(task_id: str, auto_accept: bool | None = None) -> None:
    """在 FastAPI 后台重跑任务"""
    register_task_queue(task_id)

    async def _wrap() -> None:
        from swarm.logging_config import bind_task

        with bind_task(task_id):
            try:
                await retry_task(task_id, auto_accept=auto_accept)
            finally:
                _task_handles.pop(task_id, None)

    _task_handles[task_id] = asyncio.create_task(_wrap())
