"""Brain 任务运行器 — 打通 create_task → Brain 执行 → interrupt → resume 主链路

职责:
- 单例 Brain graph（共享 MemorySaver，支持跨请求 resume）
- 后台执行 Brain 状态机，SSE 推送进度
- approve / revise / reject 通过 Command(resume=...) 恢复图执行
- 同步更新 task_records 状态
"""

from __future__ import annotations

import asyncio
import functools
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


# 阶段1（§九 TaskLedger）：异常迁至 models/errors.py（ledger 单点闸也要抛它，models 层
# 不能反向 import brain）。此处 re-export 保既有 import 路径兼容。
from swarm.models.errors import TaskTokenLimitExceeded  # noqa: E402,F401


def _ledger_effective_budget(cfg, subtask_count: int) -> int:
    """§九：ledger 云端预算=与 token 闸同源的弹性口径（base+per_subtask×子任务数）。
    base=0 保持既有关闸语义（ledger track-only）。"""
    base = int(getattr(cfg, "max_task_tokens", 0) or 0)
    if base <= 0:
        return 0
    per = int(getattr(cfg, "max_task_tokens_per_subtask", 0) or 0)
    n = int(subtask_count or 0)
    return base + per * n if (per > 0 and n > 0) else base


class TaskLockLost(Exception):
    """运行中模块锁在 Redis 侧丢失（续期返回 False）——防同模块并发写，fail-fast 中止（2nd#2）。"""

    def __init__(self, lock_key: str):
        self.lock_key = lock_key
        super().__init__(f"模块锁丢失: {lock_key}")


class TaskWallclockExceeded(Exception):
    """单次 Brain 执行墙钟超时（P1-B）——防失控任务无上限占沙箱/GPU。

    E5（阶段5）：与 TaskLockLost/TaskTokenLimitExceeded 同走资源护栏专用 except →
    _salvage_partial_from_checkpoint（有产物 PARTIAL/无产物 FAILED），不再落泛
    except 裸 FAILED 丢产物；finally 照旧释放锁/沙箱/_task_running。
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
# M-4（外部深审）：全进程订阅者总数硬上限——防成员对【多个 task】各开满 _MAX_SUBS_PER_TASK 连接
# 累积压垮整进程（内存 = 总数×队列容量、socket、每心跳周期鉴权）。默认 2000。
_GLOBAL_MAX_SUBS = max(_MAX_SUBS_PER_TASK, int(os.environ.get("SWARM_SSE_MAX_SUBS_GLOBAL", "2000")))
# 全进程当前订阅者计数（subscribe 增、unsubscribe 减）。
_global_sub_count = 0


class FanoutSubscriberLimitExceeded(Exception):
    """M-4：单 task 或全进程订阅者数达硬上限——拒绝新 SSE/WS 连接（端点转 429/关闭），防单成员无限
    开连接制造内存/socket/周期鉴权压力。区别于旧【仅软告警不拒】。"""


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
        # M-4（外部深审）：订阅者数【硬上限】——达到即拒绝新连接（抛 FanoutSubscriberLimitExceeded，
        # 端点转 429/关闭），不再仅软告警照连。旧行为：连接总数无限、流端点无专门连接上限 → 项目成员
        # 可对本 task（乃至多 task）无限开 SSE/WS 连接制造内存(N×队列容量)/socket/每心跳周期鉴权压力。
        # 双闸：单 task 上限 + 全进程总上限（防多 task 各开满累积压垮进程）。检查在建队列/改计数【之前】。
        global _global_sub_count
        if len(self._subs) >= _MAX_SUBS_PER_TASK:
            logger.warning("[FANOUT] 单 task 订阅者数达硬上限 %d，拒绝新连接（疑 SSE/WS 泄漏或滥用）",
                           _MAX_SUBS_PER_TASK)
            raise FanoutSubscriberLimitExceeded(f"单 task 订阅者数达上限 {_MAX_SUBS_PER_TASK}")
        if _global_sub_count >= _GLOBAL_MAX_SUBS:
            logger.warning("[FANOUT] 全进程订阅者总数达硬上限 %d，拒绝新连接（疑滥用/泄漏）",
                           _GLOBAL_MAX_SUBS)
            raise FanoutSubscriberLimitExceeded(f"全进程订阅者总数达上限 {_GLOBAL_MAX_SUBS}")
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
        self._subs.add(q)
        _global_sub_count += 1
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        global _global_sub_count
        if q in self._subs:
            self._subs.discard(q)
            _global_sub_count = max(0, _global_sub_count - 1)

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

# E4（阶段5，登记册 §六）：watchdog 中止登记——独立看门狗取消消费任务后，
# run_task/resume 的 CancelledError 处理据此区分「护栏中止」（→salvage）与
# 「真人工取消」（→原 CANCELLED 语义）。
_watchdog_abort: dict[str, Exception] = {}
_watchdog_tasks: dict[str, "asyncio.Task"] = {}


def _stop_watchdog(task_id: str) -> None:
    """E4：停掉任务的护栏看门狗（幂等；三入口 finally 与 stream 正常尾统一调用）。"""
    t = _watchdog_tasks.pop(task_id, None)
    if t is not None and not t.done():
        t.cancel()


async def _maybe_salvage_watchdog_abort(task_id: str, queue) -> bool:
    """E4：CancelledError 到达时查登记——watchdog 护栏中止 → salvage 并返回 True；
    真人工取消 → False（调用方原样 raise，语义不变）。"""
    _wd_exc = _watchdog_abort.pop(task_id, None)
    if _wd_exc is None:
        return False
    _t = asyncio.current_task()
    if _t is not None and hasattr(_t, "uncancel"):
        _t.uncancel()  # 3.11+：消化本次取消，后续 await（salvage 落库）不被再打断
    _kind = ("wallclock_exceeded" if isinstance(_wd_exc, TaskWallclockExceeded)
             else "module_lock_lost")
    logger.warning("[RUNNER] 任务 %s 被 watchdog 护栏中止（%s），尝试抢救已完成产物",
                   task_id, _kind)
    await asyncio.shield(_salvage_partial_from_checkpoint(
        task_id, queue,
        reason_code=_kind,
        reason_msg=f"watchdog 护栏中止（{_kind}）: {str(_wd_exc)[:200]}",
    ))
    return True

# task_id → asyncio.Task 句柄（用于 cancel）
_task_handles: dict[str, asyncio.Task] = {}

# M-2（外部深审）：调度器【停机/失主】主动中止的 task_id 集。区别于人工 cancel_task：停机中止
# 绝不能写终态 CANCELLED（CANCELLED ∈ TERMINAL_STATES → 永久出对账视野 = 假终态、丢在飞工作），
# 须保留当前活跃态并 re-raise，交对账（本副本重竞选/他副本 reconcile）恢复重派。执行 CancelledError
# 处理器据此在【停机中止】与【人工取消】间分流（对抗复核 M-2 Finding A）。
_shutdown_aborting: set[str] = set()


def mark_shutdown_abort(task_id: str) -> None:
    _shutdown_aborting.add(task_id)


def clear_shutdown_abort(task_id: str) -> None:
    _shutdown_aborting.discard(task_id)


def is_shutdown_abort(task_id: str) -> bool:
    return task_id in _shutdown_aborting

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
    "verify_runtime": "VERIFYING_RUNTIME",  # S1-4：运行时冒烟闸门（on_chain_start 自动发事件/写状态）
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


def _plan_subtask_ids(state: dict[str, Any]) -> set | None:
    """当前 plan 的子任务 id 集合（plan 对象或 dict 两态）。无 plan/无子任务返回 None。"""
    _plan_obj = state.get("plan")
    if _plan_obj is None:
        return None
    _subs = getattr(_plan_obj, "subtasks", None)
    if _subs is None and isinstance(_plan_obj, dict):
        _subs = _plan_obj.get("subtasks")
    if not _subs:
        return None
    return {
        (getattr(s, "id", None) if not isinstance(s, dict) else s.get("id"))
        for s in _subs
    }


def _count_completed_in_plan(state: dict[str, Any]) -> int:
    """当前 plan 内且 L1 通过的已完成子任务数 —— completed 语义单一事实源。

    治本(task 1bc867a1)：不能用 len(subtask_results)——它累积跨 replan/retry/rebase 的全部
    结果（含失败 + 重生成变体 + 已不在当前 plan 的旧 id），必然 > 当前 plan 的 subtask_count。
    正确语义 = 【在当前 plan 内 且 L1 通过】。_sync_task_from_state（进度分母）与 T-B token 超限
    抢救（判 PARTIAL vs FAILED）共用此口径，杜绝两处对"已完成多少"的判断漂移。
    """
    subtask_results = state.get("subtask_results")
    if not isinstance(subtask_results, dict):
        return 0
    from swarm.brain.nodes.shared import l1_passed as _passed

    plan_ids = _plan_subtask_ids(state)
    if plan_ids:
        return sum(1 for sid, out in subtask_results.items() if sid in plan_ids and _passed(out))
    return sum(1 for out in subtask_results.values() if _passed(out))


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

    if isinstance(state.get("subtask_results"), dict):
        # completed 语义单一事实源见 _count_completed_in_plan（当前 plan 内且 L1 通过）。
        done = _count_completed_in_plan(state)
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
        plan_ids = _plan_subtask_ids(state)
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

    # D07 治本：merge_conflicts 改 `is not None` 下发（含空列表清空）。brain merge 节点每轮
    # 已把 state.merge_conflicts 清成 []（第 1 轮冲突入库、恢复后第 2 轮干净合并），store.update_task
    # 用 `is not None` 本支持用 [] 清空 DB；旧 truthiness `if merge_conflicts:` 令空列表永不下发，
    # 致 DB 永久残留首轮冲突 → 任务 DONE 但 /apply-diff 永久 409。终态 _handle_post_run 走同一函数
    # 天然覆盖。★仅当 state 确带该键才下发（None=本次增量/快照未触及该字段，保留 DB 现值）。
    merge_conflicts = state.get("merge_conflicts")
    if merge_conflicts is not None:
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
    lock_holder: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any]:
    """执行 Brain 并流式推送节点事件，返回 (state values, snapshot)。

    D02 修复：模块锁经【可变容器】lock_holder 传引用，而非局部变量+返回值传回。
    plan 节点升级锁后原地写回 lock_holder["lock"]，使调用方 finally 无论本函数正常
    返回还是中途抛异常（GraphRecursionError/TaskWallclockExceeded/TaskLockLost/节点异常）
    都能释放到【当前实际持有的锁】——旧实现下升级后的新锁只存在于本函数局部变量，
    异常退出不 return 时调用方仍持已被 release 的旧锁 → 新锁无人释放泄漏死锁。
    """
    from swarm.tracing import brain_graph_config

    # 当前持有的模块锁（从容器取初值；升级后写回容器 + 本地同步，供 renew 用最新锁）。
    module_lock = lock_holder.get("lock") if lock_holder is not None else None
    # D14：renew 降频器（进入循环前初始化；首见锁=刚 acquire → 跳过首个间隔）。
    from swarm.infra.redis_client import RenewPacer
    _renew_pacer = RenewPacer()

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
    _progress_plan_gen: int | None = None  # F5：进度单调的计划代际界
    # P1-B：本次执行段墙钟起点 + 弹性预算。每个图事件循环顶检查；超【弹性】上限 → raise →
    # 归一化 FAILED + 释放资源。★弹性随规划揭示的子任务数动态放宽，绝不误杀合法大型任务★。
    _wc_cfg = get_config()
    _wc_base_s = _wc_cfg.task_deadline_s
    _wc_per_subtask_s = _wc_cfg.task_deadline_per_subtask_s
    _wc_subtasks = _subtask_n or 0  # 规划前多为 0（只用 base）；下方循环见 plan 输出后放宽
    _wc_start = time.monotonic()

    # D26 治本：节点 on_chain_end 的 output 是【增量 patch】（只含本节点写出的键），而
    # _sync_task_from_state 按【全量 state】记三本账（completed/abandoned/plan）。直接喂增量
    # 会系统性错账：
    #  (a) dispatch 的 output 恒含 subtask_results 但从不含 abandoned_subtask_ids/give_up_isolated_ids
    #      （那是 handle_failure 的键）→ 每次 dispatch 同步都算 abandoned=0 写库，把 handle_failure
    #      刚放弃的 N 个清零；
    #  (b) dispatch/merge output 通常不含 plan → _plan_subtask_ids 为 None → completed 退化为数全部
    #      累积结果（含 replan 后已不在当前 plan 的旧 id）且夹紧失效 → DB completed > subtask_count。
    # 修法：维护跨事件累积的全量快照。BrainState 记账键（plan/subtask_results/abandoned_subtask_ids/
    # give_up_isolated_ids/merge_conflicts…）均为默认 last-write-wins 通道——每次写出的即该键全量值，
    # 故 .update() 累积得到当前真全量。同步据【累积快照】而非裸增量 output，保住不变量：abandoned/
    # completed 只被"真知当前全量值"的写入更新，绝不被不含该键的增量覆盖成 0；同时保留中途进度可见性
    # （每个 _SYNC_ON_NODES 节点结束仍即时下发累积快照）。resume 时用 DB 已存 plan 播种，令 completed
    # 过滤在 plan 节点不重跑的情形下仍有分母。
    _accumulated_state: dict[str, Any] = {}
    if isinstance(_plan_rec, dict) and _plan_rec:
        _accumulated_state["plan"] = _plan_rec

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

    # §九 阶段1.4：预算脊柱登记——执行段起点 attach（resume/重启由 attach 从 DB 恢复已
    # 结算额度延续）；预算=云端弹性口径（base+per_subtask×已知子任务数，与 token 闸同源），
    # base=0 保持既有关闸语义（track-only）。stage 随下方 on_chain_start 节点名流转。
    from swarm.models import ledger as _ledger
    _ledger.attach(task_id, budget_total=_ledger_effective_budget(_wc_cfg, _wc_subtasks))

    # E4（阶段5）：独立护栏看门狗——旧行为墙钟/锁续期全挂在图事件循环顶（事件驱动），
    # 节点内单个 await 悬挂（LLM 黑洞/沙箱死连接）= 无事件 = 零保护 + 锁静默过期。
    # 看门狗按时钟驱动（15s 周期）：超墙钟/锁续期失败 → 登记护栏异常 + 取消消费任务，
    # run_task 的 CancelledError 分支据登记走 salvage（与 E5 同终点）。循环内既有搭车
    # 检查保留（belt-and-suspenders；RenewPacer 共享去重，不会双 renew）。
    _consumer_task = asyncio.current_task()

    async def _guard_watchdog() -> None:
        try:
            while True:
                await asyncio.sleep(15.0)
                # 5.9 猎手 F3（MEDIUM）：tick 内任何非预期异常（renew 未吞的意外/配置读取）
                # 绝不让 watchdog 静默死亡——护栏死了=E4 要治的"静默失锁"换形态回归。
                try:
                    _eff = _effective_deadline_s(_wc_base_s, _wc_per_subtask_s, _wc_subtasks)
                except Exception as _wd_tick_exc:  # noqa: BLE001
                    logger.error("[E4] watchdog tick 异常（护栏继续跑，下轮重试）: %s", _wd_tick_exc)
                    continue
                try:
                    _raise_if_wallclock_exceeded(_wc_start, _eff)
                except TaskWallclockExceeded as _exc:
                    logger.warning("[E4] watchdog：任务 %s 超弹性墙钟（事件循环可能悬挂）→ 取消执行走 salvage", task_id)
                    _watchdog_abort[task_id] = _exc
                    if _consumer_task is not None and not _consumer_task.done():
                        _consumer_task.cancel()
                    return
                try:
                    _lk = (lock_holder or {}).get("lock") or module_lock
                    if _lk is not None and _renew_pacer.due(_lk) \
                            and not await asyncio.to_thread(_lk.renew):
                        logger.warning("[E4] watchdog：任务 %s 模块锁续期失败（防并发写树）→ 取消执行走 salvage", task_id)
                        _watchdog_abort[task_id] = TaskLockLost(getattr(_lk, "key", str(_lk)))
                        if _consumer_task is not None and not _consumer_task.done():
                            _consumer_task.cancel()
                        return
                except Exception as _wd_renew_exc:  # noqa: BLE001 — F3：意外异常不杀护栏
                    logger.error("[E4] watchdog 锁续期段异常（护栏继续跑）: %s", _wd_renew_exc)
        except asyncio.CancelledError:
            return

    _stop_watchdog(task_id)  # 防上次残留（resume 复用同 task_id）
    _wd_t = asyncio.create_task(_guard_watchdog())

    def _wd_done(t: "asyncio.Task") -> None:
        # F3：非取消退出必须 loud（fire-and-forget 的 "never retrieved" 只在 GC 时才响）
        if not t.cancelled() and t.exception() is not None:
            logger.error("[E4] watchdog 异常终结（任务 %s 失去墙钟/锁续期护栏）: %s",
                         task_id, t.exception())

    _wd_t.add_done_callback(_wd_done)
    _watchdog_tasks[task_id] = _wd_t

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
        # 无额外后台任务，进程处理图事件时顺带 renew（Redis 关闭时 no-op）。
        # D14 治本：renew 是同步 Redis IO——①降频（RenewPacer：距上次不足 TTL/10 跳过；锁升级
        # 换对象时重置计时不补 renew，新锁刚 acquire 即满 TTL），不再每事件一次；②卸线程池
        # （asyncio.to_thread）——即便 Redis 网络黑洞，socket 超时内的阻塞也只占工作线程，
        # 事件循环/其它任务/SSE/API 不陪等。renew() 自身吞通信异常只返 bool，to_thread 不改变
        # 异常传播语义（renew 内部未捕获的意外异常仍照旧经 except Exception → FAILED）。
        # ★对抗复核 2nd#2 治本★：renew 返回 False = 锁已在 Redis 侧丢失（另一任务可能已抢占同
        # 模块并发写工作树）→ 不能再继续，fail-fast 中止本任务（经 except Exception → FAILED +
        # finally 释放资源）。内存兜底/未启用 Redis 时 renew 恒 True，不触发（单进程无跨进程互斥意义）。
        if module_lock is not None and _renew_pacer.due(module_lock) \
                and not await asyncio.to_thread(module_lock.renew):
            await _emit(queue, {
                "step": "lock_lost", "status": "failed",
                "message": "模块锁已失效（TTL 超期/Redis 抖动），为防同模块并发写已中止任务",
                "mode": "brain", "progress": 100,
            })
            raise TaskLockLost(getattr(module_lock, "key", "?"))
        kind = event.get("event", "")
        if kind == "on_chain_start":
            name = event.get("name", "")
            # §九 阶段1.4：预算阶段随节点流转（未知节点保持上一阶段，不重置）。
            _stg = _ledger.stage_for_node(name)
            if _stg:
                _ledger.set_stage(task_id, _stg)
            if name and name not in ("LangGraph", "ChannelWrite", "increment_retry"):
                # E10（阶段5，登记册 §六）：进度由 completed/count 派生——旧 min(+4,90)
                # 与完成度无关（几个节点后恒 90% 挂满全程）。规划期（plan 未知）按节点
                # 缓慢爬坡帽 25；执行期=15+75×完成占比（单调不回退，帽 90 留给交付段）。
                # 5.9 猎手 F1（CRITICAL）：_plan_subtask_ids 无 plan 返回 None——len(None)
                # 会让每个 fresh/retry 任务在首个节点事件 TypeError 整单 FAILED。
                _p_ids = _plan_subtask_ids(_accumulated_state)
                _p_total = len(_p_ids) if _p_ids else 0
                if _p_total:
                    # 5.9 猎手 F5：单调约束以【计划代际】为界——replan 换代（子任务 id 集变）
                    # 时允许基线重置，否则 max() 把进度钉死在旧高位（用户看 90% 卡死，
                    # 实际在重做；DB completed 却在掉=两信号打架）。代内保持单调。
                    _p_gen = hash(frozenset(_p_ids))
                    if _p_gen != _progress_plan_gen:
                        _progress_plan_gen = _p_gen
                        progress = min(progress, 25)  # 换代=回执行起跑线（可观测的诚实回退）
                    _p_done = _count_completed_in_plan(_accumulated_state)
                    progress = max(progress, min(15 + int(75 * _p_done / _p_total), 90))
                else:
                    progress = min(progress + 4, 25)
                status = _NODE_STATUS_MAP.get(name)
                if status:
                    # round27 perf：本函数是每图事件的热路径，psycopg 同步调用会卡整个事件环
                    # （并发任务+SSE+API 全部陪等）→ 卸线程池。顺序语义不变（await 保序）。
                    await asyncio.to_thread(store.update_task, task_id, status=status)
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
            # D26：先把本节点增量 output 并入累积全量快照（含 handle_failure 等不在 _SYNC_ON_NODES
            # 的节点——其放弃 id 需在后续 dispatch 同步时可见，不被清零）。
            if isinstance(output, dict):
                _accumulated_state.update(output)
            if name in _SYNC_ON_NODES and isinstance(output, dict):
                # round27 perf：同上，DB 回写卸线程池，不卡事件环。
                # D26：喂【累积全量快照】而非裸增量 output——记账（completed/abandoned/plan）方正确。
                await asyncio.to_thread(_sync_task_from_state, task_id, dict(_accumulated_state))
                # P1-B：规划/拆分揭示子任务数 → 放宽弹性墙钟（只增不减，防大型任务被基线上限误杀）。
                _plan_out = output.get("plan")
                _subs = None
                if hasattr(_plan_out, "subtasks"):
                    _subs = getattr(_plan_out, "subtasks", None)
                elif isinstance(_plan_out, dict):
                    _subs = _plan_out.get("subtasks")
                if isinstance(_subs, list) and len(_subs) > _wc_subtasks:
                    _wc_subtasks = len(_subs)
                    # §九 阶段1.4：弹性预算随规划揭示的规模放宽（与墙钟/token 闸同步）。
                    # R38 复核 F1：改 widen_budget（单调只增）——per_subtask×n 可能小于
                    # STAGE1 已按 per_module×n 放宽的值（模块粗、子任务少时），set_budget
                    # 覆写会在同一执行段内把安全余量收缩回去，复现 round38 死点。
                    _ledger.widen_budget(
                        task_id, _ledger_effective_budget(_wc_cfg, _wc_subtasks))
            # §九 阶段1.4：replan 轮次入账（按 state 绝对值同步，防重复累加）。
            if isinstance(output, dict) and isinstance(output.get("replan_count"), int):
                _ledger.set_replan_rounds(task_id, output["replan_count"])
            if name in _TOKEN_GATE_NODES and isinstance(output, dict):
                # round27 perf：get_task + 闸门检查（内含估算与可能的 DB 落 FAILED）卸线程池。
                fresh = (await asyncio.to_thread(store.get_task, task_id)) or task_rec
                ok, usage = await asyncio.to_thread(
                    functools.partial(
                        store.check_task_token_limit,
                        task_id,
                        description=fresh.get("description") or "",
                        merged_diff=output.get("merged_diff") or fresh.get("merged_diff") or "",
                        subtask_results=output.get("subtask_results"),
                        # round27 弹性预算：复用墙钟维护的子任务数（规划揭示后放宽，与 P1-B 同理）
                        subtask_count=_wc_subtasks,
                    )
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
                        # E3（登记册 §六）：升级失败=目标模块被其它任务持有——有界等待
                        # 重试（对方释放即升级成功），绝不保留旧锁照跑（旧"default"与
                        # 他人模块键零互斥=纸面锁，两任务并发写同一 git 树）。耗尽预算
                        # fail-loud（重试经 E1 播种低成本续跑）。await 节拍不冻结事件循环。
                        from swarm.infra.redis_client import ModuleLockUpgradeConflict
                        try:
                            _lk_wait = float(os.environ.get(
                                "SWARM_MODULE_LOCK_UPGRADE_WAIT_S", "300") or "300")
                        except ValueError:
                            _lk_wait = 300.0
                        _lk_deadline = asyncio.get_running_loop().time() + _lk_wait
                        while True:
                            try:
                                module_lock = upgrade_module_lock(module_lock, pid, plan_dict)
                                break
                            except ModuleLockUpgradeConflict as _lk_exc:
                                if asyncio.get_running_loop().time() >= _lk_deadline:
                                    logger.error(
                                        "[ModuleLock] E3 升级冲突等待超预算(%.0fs)仍未解: %s"
                                        "——fail-loud（绝不纸面互斥照跑），任务可 retry 续跑",
                                        _lk_wait, _lk_exc)
                                    raise
                                logger.info("[ModuleLock] E3 升级冲突，%s——3s 后重试", _lk_exc)
                                await asyncio.sleep(3)
                        # D02：升级后的新锁立即写回容器——此后任何异常退出，调用方 finally
                        # 都能经 lock_holder 释放到新锁（旧锁已在 upgrade 内 release）。
                        if lock_holder is not None:
                            lock_holder["lock"] = module_lock

    _stop_watchdog(task_id)  # E4：正常收尾即停（异常路径由三入口 finally 兜底）
    snapshot = await graph.aget_state(config)
    final_state = dict(snapshot.values) if snapshot and snapshot.values else {}
    return final_state, snapshot


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
        # R52-1（round52 实锤=外审#14 家族）：REJECT ≠ 一律 FAILED。escalate 家族的
        # REJECT（failure_escalated/failed_subtasks/l2_failed）只说明【任务整体】没
        # 达成功终态；若当前 plan 内已有 L1 通过的完成产出（round52 实测 16 个被本
        # 分支整体丢弃），诚实终态=PARTIAL（列明需人工补完），与 HANDLE_FAILURE 一路
        # 承诺的口径一致。绝不放行为 DONE（human_decision 仍 REJECT、不走
        # LEARN_SUCCESS）；仅虚假前提/计划无效类（产出本身不可信）与零产出维持 FAILED。
        _completed_n = _count_completed_in_plan(state)
        _partial_eligible = (
            _completed_n > 0
            and _vf != "plan_invalid"
            and not str(reason).startswith("clarification_required")
            and not state.get("clarify_blocked_by_facts")
        )
        if _partial_eligible:
            logger.warning(
                "[RUNNER] R52-1 REJECT 但 plan 内已有 %d 个 L1 通过产出 → 诚实 PARTIAL"
                "（拒因存档 error，不丢已完成工作）: %s", _completed_n, reason)
            store.update_task(
                task_id, status="PARTIAL", error=f"partial(rejected): {reason}"[:300],
                token_usage=_failed_machine_account(task_id, state, "rejected_partial"))
            _emit_task_notification(task_id, _rec, "PARTIAL")
            audit("task_partial", orchestrator="Brain", task_id=task_id,
                  project_id=_rec.get("project_id"),
                  error=f"partial(rejected): {reason}"[:300])
            await _emit(queue, {
                "step": "done", "status": "partial",
                "message": f"部分交付（{_completed_n} 个完成产出保留；拒因：{reason[:160]}）",
            })
            return
        # R38-E 复核 F7：所有 FAILED 写入都带机读账（error+ledger 快照），audit 不再是唯一去处
        store.update_task(task_id, status="FAILED", error=f"rejected: {reason}"[:300],
                          token_usage=_failed_machine_account(task_id, state, "rejected"))
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
            store.update_task(
                task_id, status="FAILED",
                error=f"delivery_not_accepted: {_reason}"[:300],
                token_usage=_failed_machine_account(task_id, state, "delivery_not_accepted"))
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
    _attach_observability_account(token_usage, state)  # G3-1：DONE 终态机读键不再 API 全盲
    duration = store.compute_task_duration_seconds(task_rec)
    # 部分交付：有子任务被放弃(重试耗尽)或保 build 放弃(阶梯三 revert/桩)→ 终态 PARTIAL(非 DONE)。
    # 已完成子任务的真实产物照常落盘/合并/过 L2，但任务【诚实标未完成】，列明放弃/桩项——绝不当
    # DONE 假成功。give_up_isolated_ids 是阶梯三保 build 放弃的子任务（本地树已清/打桩，build 未毒），
    # 与 abandoned（重试耗尽连坐放弃）合并判 PARTIAL。
    from swarm.brain.gates import delivery_incomplete, partial_delivery_ids, terminal_status
    _abandoned = state.get("abandoned_subtask_ids") or []
    _given_up = state.get("give_up_isolated_ids") or []
    _rebase_dropped = state.get("merge_rebase_dropped") or []  # 复核 H-1：rebase 超限丢弃的子任务
    _partial_ids = partial_delivery_ids(state)  # 单一事实源：abandoned ∪ give_up ∪ rebase_dropped
    # X-1 残留（外部深审）：交付 apply 全失败/不完整 = merged_diff 没（全部）落到项目树——subtask
    # 都成功但产物没进用户项目，绝不能报 DONE 假成功。这是子任务之外的【任务级】交付失败信号。
    _delivery_incomplete = delivery_incomplete(state)
    _final_status = terminal_status(state)  # 单一裁决：_partial_ids ∪ 交付失败 → PARTIAL
    # 5.9 猎手 F4：终态写拆两步——记账字段先落（不受 E2 CAS 限制），status 再 CAS。
    # 否则收尾窗口撞 cancel/reconcile 时整行被拒，token/duration 记账连坐蒸发（§九账本口径受损）。
    store.update_task(
        task_id,
        token_usage=token_usage,
        duration_seconds=round(duration, 2) if duration is not None else None,
    )
    store.update_task(task_id, status=_final_status)
    _emit_task_notification(task_id, task_rec, _final_status)
    output_parts = _build_result_payload(state)
    if _final_status == "PARTIAL":
        logger.warning("[RUNNER] 任务 %s 部分交付(PARTIAL)：放弃 %d 个(重试耗尽 %s) + 保 build 放弃 %d 个(阶梯三 %s)"
                       " + rebase 超限丢弃 %d 个(%s) + 交付未落盘=%s",
                       task_id, len(_abandoned), _abandoned, len(_given_up), _given_up,
                       len(_rebase_dropped), _rebase_dropped, _delivery_incomplete)
        # hunter F3：交付失败为唯一 PARTIAL 成因时，别用"已完成子任务真实落盘"打头（那指 worker
        # 工作区，与"产物未落项目树"读来矛盾）——改用交付面开场，避免误导。
        if _delivery_incomplete and not (_abandoned or _given_up or _rebase_dropped):
            _msg = "部分交付：子任务已完成且过 L2，但合并产物未（全部）落入项目工作树"
        elif _abandoned:
            _msg = f"部分交付：已完成子任务真实落盘且可构建(已过 L2)；放弃 {len(_abandoned)} 个(重试耗尽)：{_abandoned}"
        else:
            _msg = "部分交付：已完成子任务真实落盘且可构建(已过 L2)"
        if _given_up:
            _msg += f"；保 build 放弃 {len(_given_up)} 个(本地树已清/打桩，需人工补完)：{_given_up}"
        if _rebase_dropped:
            # 复核 H-1：否则 rebase-only PARTIAL 会显示"放弃 0 + 保 build 0"无解释。
            _msg += f"；merge rebase 超限丢弃 {len(_rebase_dropped)} 个(rebased 变更未并入，需人工核验)：{_rebase_dropped}"
        if _delivery_incomplete:
            # X-1 残留：交付 apply 失败=产物没（全部）落到项目树，须显式说明（否则 PARTIAL 无解释）。
            _msg += "；⚠️交付 apply 失败：合并产物未（全部）落入项目工作树，需人工核验/重新交付（详见 degraded_reasons）"
        # D18：终态载荷并入 complete 事件。旧协议 complete 后再发 step:"result"，但 SSE/WS
        # 订阅端在 complete 即 break → result（merged_diff/l3 等）永远送不到（死协议）。
        # CLI 原生消费 complete 事件内的 result 键（cli/__init__.py），WebUI 靠 complete 后
        # REST 重载详情——并入 complete 对下游零破坏且载荷真正可达。
        await _emit(queue, {
            "step": "complete",
            "status": "partial",
            "message": _msg,
            "mode": "brain",
            "progress": 100,
            "result": output_parts,
        })
    else:
        await _emit(queue, {
            "step": "complete",
            "status": "done",
            "message": "任务执行完成",
            "mode": "brain",
            "progress": 100,
            "result": output_parts,
        })


def build_degraded_summary(degraded_reasons) -> dict[str, int]:
    """F2（阶段7）：degraded 留痕 → 机读汇总（按前缀聚合计数）。

    E2E 判读脚本直接读 result.degraded_summary 即可回答"这轮降级了什么、各多少次"，
    不再从日志考古。前缀 = 第一个 ':' 之前（约定俗成的机制名，如
    requirements_extract / acceptance_skipped / merge_secret_reported）。"""
    out: dict[str, int] = {}
    for r in (degraded_reasons or []):
        prefix = str(r).split(":", 1)[0].strip() or "(empty)"
        out[prefix] = out.get(prefix, 0) + 1
    return out


def _build_result_payload(state: dict[str, Any]) -> dict[str, Any]:
    output_parts: dict[str, Any] = {}
    for key in ("merged_diff", "l2_passed", "learn_summary", "complexity", "plan", "subtask_results", "human_decision", "learned", "knowledge_context", "merge_conflicts", "l3_passed", "l3_skipped", "l3_message", "plan_validation_issues", "plan_validation_warnings", "shared_contract", "verification_failure"):
        val = state.get(key)
        if val is None or val == "" or val == {}:
            continue
        if hasattr(val, "model_dump"):
            output_parts[key] = val.model_dump(mode="json")
        elif isinstance(val, dict):
            output_parts[key] = val
        else:
            output_parts[key] = str(val) if not isinstance(val, (bool, int, float)) else val
    # F2（阶段7）：degraded 全量留痕 + 机读汇总双出——判读脚本读 summary，人工看明细
    _dg = list(state.get("degraded_reasons") or [])
    if _dg:
        output_parts["degraded_reasons"] = _dg
        output_parts["degraded_summary"] = build_degraded_summary(_dg)
    return output_parts


async def _load_state_snapshot(task_id: str, thread_id: str | None = None) -> dict[str, Any] | None:
    """读任务 LangGraph 实时快照的 state values（纯读，不推进图）。取不到返回 None。

    与 get_pending_interrupt 同构：token 超限从 dispatch/merge 等节点 end raise 时，该节点已
    完成并被 checkpoint → 此处能取回含已完成子任务产物的 state，供 PARTIAL 抢救判定。
    """
    from swarm.tracing import brain_graph_config

    graph = get_compiled_brain_graph()
    task_rec = store.get_task(task_id) or {}
    thread_id = thread_id or task_rec.get("thread_id") or task_id  # E1：可指定历史 thread
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
    except Exception as exc:  # noqa: BLE001 — 读快照失败按"取不到"处理，退回 FAILED 兜底
        logger.warning("[RUNNER] 任务 %s 读 checkpoint 快照失败: %s", task_id, exc)
        return None
    if not snapshot or not getattr(snapshot, "values", None):
        return None
    return dict(snapshot.values)


def _attach_observability_account(token_usage: dict[str, Any],
                                  state: dict[str, Any] | None) -> dict[str, Any]:
    """G3-1（round38c 主题G P0）：round38 机读键并进 token_usage jsonb 槽——三终态统一。

    此前 degraded_summary 只进 SSE result payload 与 PARTIAL/FAILED 账，DONE 终态
    API 全盲；contract_failed_modules/l2_details/validate 降级标记只活在 LangGraph
    state（SSE+API 双盲）——round38 造这些键就是给盯跑脚本的，DONE 路径必须读得到。
    与 R38-E 机读账同槽同口径，零迁移。
    R40-2（round40 实证）：ledger 权威账（stage_spent/llm_calls/cloud in-out）此前只在
    FAILED 路径合并（_failed_machine_account），PARTIAL/DONE 终态这些键全缺——本函数
    是三终态唯一共同出口，快照合并收编到这里（只补缺失键绝不覆写，FAILED 先填不冲突）。"""
    st = state or {}
    _tid = str(st.get("task_id") or "").strip()
    if _tid:
        try:
            from swarm.models import ledger as _att_ledger
            _snap = _att_ledger.snapshot(_tid) or {}
            for _k in ("cloud_tokens_in", "cloud_tokens_out", "local_tokens",
                       "llm_calls", "stage_spent", "budget_total"):
                if _k in _snap and _k not in token_usage:
                    token_usage[_k] = _snap[_k]
        except Exception as _snap_exc:  # noqa: BLE001 — 观测账增强面，绝不阻断终态写
            logger.warning("[RUNNER] 终态账 ledger 快照合并失败（跳过）: %s", _snap_exc)
    _dg = list(st.get("degraded_reasons") or [])
    if _dg:
        token_usage["degraded_summary"] = build_degraded_summary(_dg)
        token_usage.setdefault("degraded_reasons", _dg[:50])
    _cfm = st.get("contract_failed_modules")
    if _cfm:
        token_usage["contract_failed_modules"] = list(_cfm)[:30]
    _l2d = st.get("l2_details")
    if isinstance(_l2d, dict) and _l2d.get("issues"):
        token_usage["l2_issues_head"] = [str(i)[:300] for i in _l2d["issues"][:5]]
    try:
        _vd = sorted(
            sid for sid, wo in (st.get("subtask_results") or {}).items()
            if (getattr(wo, "l1_details", None) or {}).get("build_cmd_downgraded_to_validate"))
        if _vd:
            token_usage["validate_downgraded_subtasks"] = _vd[:30]
    except Exception:  # noqa: BLE001 — 观测账增强面，绝不阻断终态写
        pass
    return token_usage


def _failed_machine_account(task_id: str, state: dict[str, Any] | None,
                            reason_code: str) -> dict[str, Any]:
    """R38-E：FAILED 终态的机读账（复用 token_usage jsonb 槽，与 PARTIAL 对称）。

    ledger.snapshot 是权威真账（cloud in/out/llm_calls/stage_spent）——salvage 时
    entry 尚在内存（detach 在 run_task finally）；取不到（极端时序）则空账不阻断。
    degraded_summary 与 deliver payload 同口径（build_degraded_summary）。"""
    tu: dict[str, Any] = {"salvage_reason": reason_code}
    try:
        from swarm.models import ledger as _fma_ledger
        snap = _fma_ledger.snapshot(task_id) or {}
        if not snap:
            # R38 复核 F6：entry 已 detach（非异常路径）同样可观测——空账≠没花钱
            logger.warning("[RUNNER] 任务 %s FAILED 机读账：ledger 无在内存条目（已 detach？）"
                           "→ 仅带 salvage_reason 空账", task_id)
        for k in ("cloud_tokens_in", "cloud_tokens_out", "local_tokens",
                  "llm_calls", "stage_spent", "budget_total"):
            if k in snap:
                tu[k] = snap[k]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RUNNER] 任务 %s FAILED 机读账取 ledger 快照失败（空账继续）: %s",
                       task_id, exc)
    _dg = (state or {}).get("degraded_reasons") or []
    if _dg:
        tu["degraded_summary"] = build_degraded_summary(_dg)
        tu["degraded_reasons"] = list(_dg)[:50]
    # A2 复核残留：token 闸预写的 limit_exceeded/limit 诊断键不被本账覆写丢失——
    # H3 合并原来只在 PARTIAL 分支生效，FAILED 分支整体覆写会抹掉"因预算闸而死"
    # 的机读归因（与 PARTIAL 对称补齐）。
    try:
        _prev = (store.get_task(task_id) or {}).get("token_usage") or {}
        if isinstance(_prev, dict):
            for _k in ("limit_exceeded", "limit"):
                if _k in _prev:
                    tu[_k] = _prev[_k]
    except Exception:  # noqa: BLE001 — 诊断键补齐失败不阻断终态账
        pass
    _attach_observability_account(tu, state)  # G3-1：FAILED 同享机读键（幂等）
    return tu


async def _finalize_governor_partial(
    task_id: str,
    state: dict[str, Any],
    queue: _FanoutTopic,
    *,
    reason_code: str,
    reason_msg: str,
) -> str:
    """资源护栏（token 预算/…）中止后的终态归一化：有已完成产物→PARTIAL 抢救，无→FAILED。

    T-B（round28）：token 闸门在 dispatch/merge 等 9 个节点 end raise TaskTokenLimitExceeded，
    旧路径冒泡到泛 except → 无脑 FAILED，跳过 PARTIAL 组装 → 已 L1 通过、真实落盘/合并的子任务
    产物被整单丢弃（round28 实测 4/55 撞闸丢完整 ruoyi-alarm 模块）。云端预算是【成本护栏】而非
    交付失败，绝不该连坐丢已产出工作。判据 = 当前 plan 内 L1 通过的完成数（单一事实源
    _count_completed_in_plan）：>0 → 诚实 PARTIAL（列明因预算中止、余下需重跑续做，可 retry）；
    =0 → 仍 FAILED（无可抢救产物，不伪造 PARTIAL）。返回最终 status。
    """
    _rec = store.get_task(task_id) or {}
    # 先持久化已推进的 plan/completed 计数（与正常路径同源，保证 WebUI 分母/进度不回退）。
    try:
        _sync_task_from_state(task_id, state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RUNNER] 任务 %s 抢救前回写 state 失败（不阻断终态判定）: %s", task_id, exc)

    completed = _count_completed_in_plan(state)
    if completed <= 0:
        # R38-E：FAILED 终态也带机读账——error 串 + ledger 权威快照 + degraded_summary
        # 落任务记录（round38 实测：audit 有账但 API task.error=None/token_usage={}，
        # 违背 runbook §6"收尾第一步先读机读账"）。
        _err = f"{reason_code}: 无已完成子任务可交付"[:300]
        _tu = _failed_machine_account(task_id, state, reason_code)
        store.update_task(task_id, status="FAILED", error=_err, token_usage=_tu)
        _emit_task_notification(task_id, _rec, "FAILED")
        audit("task_failed", orchestrator="Brain", task_id=task_id,
              project_id=_rec.get("project_id"), error=_err)
        await _emit(queue, {
            "step": "error", "status": "error",
            "message": f"{reason_msg}；无已完成子任务可交付，任务失败",
            "mode": "brain", "progress": -1,
        })
        return "FAILED"

    token_usage = store.estimate_token_usage(
        description=_rec.get("description") or state.get("task_description") or "",
        merged_diff=state.get("merged_diff") or "",
        subtask_results=state.get("subtask_results"),
    )
    # 复核 H3（阶段1）：check_task_token_limit 先写的 limit_exceeded/limit 诊断标记
    # 不被本次 token_usage 覆盖丢失；并补 salvage 归因，终态记录可机读区分
    # "预算闸 PARTIAL"与其他抢救原因（原来只剩自由文本日志）。
    _prev_tu = _rec.get("token_usage") or {}
    if isinstance(_prev_tu, dict):
        for _k in ("limit_exceeded", "limit"):
            if _k in _prev_tu:
                token_usage[_k] = _prev_tu[_k]
    token_usage["salvage_reason"] = reason_code
    _attach_observability_account(token_usage, state)  # G3-1：三终态统一机读键
    duration = store.compute_task_duration_seconds(_rec)
    # F4 同款拆两步：记账先落，status 再 CAS
    store.update_task(
        task_id,
        token_usage=token_usage,
        duration_seconds=round(duration, 2) if duration is not None else None,
    )
    _partial_row = store.update_task(task_id, status="PARTIAL")
    if _partial_row is None:
        # 5.9 复核 #4残留①：CAS 拒绝（任务已被 cancel 等推入终态）——不发"部分交付"
        # 假通知（DB=CANCELLED 而用户收到 PARTIAL=观测面自相矛盾）。
        logger.warning("[RUNNER] 任务 %s salvage PARTIAL 写被终态守卫拒绝（已是终态），跳过通知", task_id)
        return
    _emit_task_notification(task_id, _rec, "PARTIAL")
    audit("task_partial", orchestrator="Brain", task_id=task_id,
          project_id=_rec.get("project_id"),
          error=f"{reason_code}: 抢救 {completed} 个已完成子任务为部分交付"[:300])
    logger.warning("[RUNNER] 任务 %s %s → 抢救 %d 个已完成子任务为 PARTIAL（余下未完成，可重跑续做）",
                   task_id, reason_code, completed)
    # D18：终态载荷并入 complete（与 _handle_post_run 正常终态同协议，独立 result 事件已废）。
    await _emit(queue, {
        "step": "complete", "status": "partial",
        "message": (f"{reason_msg}；已抢救 {completed} 个已完成子任务(真实落盘)为部分交付，"
                    f"余下未完成，重跑可续做"),
        "mode": "brain", "progress": 100,
        "result": _build_result_payload(state),
    })
    return "PARTIAL"


async def _salvage_partial_from_checkpoint(
    task_id: str,
    queue: _FanoutTopic,
    *,
    reason_code: str,
    reason_msg: str,
) -> None:
    """资源护栏中止的统一抢救入口：取 checkpoint state → _finalize_governor_partial。

    checkpoint 取不到（PG 抖动/无快照）→ 退回 FAILED（不比旧路径更差，但绝不静默 DONE）。
    """
    # 5.9 复核 新发现A：先停 watchdog——inline 护栏 raise 后 watchdog 仍活着，
    # 下一 tick 对"墙钟已超"恒真 → cancel 打进 salvage 中途 → 任务留非终态丢产物。
    _stop_watchdog(task_id)
    state = await _load_state_snapshot(task_id)
    if not state:
        _rec = store.get_task(task_id) or {}
        # R38-E：兜底 FAILED 同样落机读账（error + ledger 快照），不留裸 FAILED。
        _err = f"{reason_code}: checkpoint 不可读"[:300]
        store.update_task(task_id, status="FAILED", error=_err,
                          token_usage=_failed_machine_account(task_id, None, reason_code))
        _emit_task_notification(task_id, _rec, "FAILED")
        audit("task_failed", orchestrator="Brain", task_id=task_id,
              project_id=_rec.get("project_id"), error=_err)
        await _emit(queue, {
            "step": "error", "status": "error",
            "message": f"{reason_msg}；无法读取执行快照抢救产物，任务失败",
            "mode": "brain", "progress": -1,
        })
        return
    await _finalize_governor_partial(
        task_id, state, queue, reason_code=reason_code, reason_msg=reason_msg,
    )


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

    # E11（2026-07-09 登记册）：run_task 本身就是执行入口（调度器 dequeue 后调用/直调），
    # 原此处把自己再入队=幽灵队列项，调度器稍后 dequeue 到时任务已在跑/已终态，全靠三层
    # 去重兜底。DB 是权威源（reconcile 把 PENDING 重新入队），删除不丢工作信号。
    from swarm.infra.redis_client import ModuleLock

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
    # D02：锁经可变容器传入 _stream_brain_events，plan 升级锁后原地写回，finally 始终释放【当前】锁。
    lock_holder: dict[str, Any] = {"lock": module_lock}
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

        # E1（阶段5，登记册 §六）：retry 播种——旧行为 retry 换 thread 从零重跑，已
        # L1 通过的真产物整批作废（checkpoint 只服务人工闸 resume）。这里读【上一执行
        # 段】checkpoint，把 plan+L1 通过产物+覆盖水位 seed 进 initial_state：plan 节点
        # 的 replan 保留机制（签名一致/A11 scope 认领）自然免重做已付工作，覆盖单调
        # 合同跨 retry 延续。播种失败=纯增益降级（从零重跑=旧行为）；指针一次性消费。
        _prev_thread = str(task_rec.get("retry_prev_thread_id") or "").strip()
        if _prev_thread:
            try:
                from swarm.brain.nodes.shared import l1_passed as _e1_l1p
                _prev_state = await _load_state_snapshot(task_id, thread_id=_prev_thread)
                _prev_results = (_prev_state or {}).get("subtask_results") or {}
                _kept = {sid: out for sid, out in _prev_results.items() if _e1_l1p(out)}
                # 5.9 复核 #6：requirement_items 一起播种——extract 节点幂等跳过使
                # req-id（内容 hash of LLM 复述文本）逐字延续，否则重抽措辞漂移=
                # id 换代，水位单调合同跨 retry 形同虚设（求交后被静默滤掉）。
                _ri = (_prev_state or {}).get("requirement_items")
                _wm = (_prev_state or {}).get("coverage_watermark")
                if not _kept or (_prev_state or {}).get("plan") is None:
                    logger.info(
                        "[E1] retry 播种：上一执行段（thread=%s）无可播种产物"
                        "（可能被 checkpoint GC 清理/无 L1 通过产物）→ 从零重跑", _prev_thread)
                if _kept and (_prev_state or {}).get("plan") is not None:
                    initial_state["plan"] = _prev_state["plan"]
                    initial_state["subtask_results"] = _kept
                    if _ri:
                        initial_state["requirement_items"] = list(_ri)
                    if _wm:
                        initial_state["coverage_watermark"] = list(_wm)
                    logger.info(
                        "[E1] retry 播种：携带上一执行段 %d 个已 L1 通过产物 + 覆盖水位 %d 条"
                        "（新计划经签名/scope 认领免重做）", len(_kept), len(_wm or []))
            except Exception as exc:  # noqa: BLE001 — 播种是增益，失败降级从零重跑
                logger.warning("[E1] retry 播种失败（降级从零重跑，行为=旧版）: %s", exc)
            finally:
                try:
                    store.update_task(task_id, retry_prev_thread_id="")
                except Exception:  # noqa: BLE001
                    pass

        thread_id = task_rec.get("thread_id") or task_id
        store.update_task(task_id, status="ANALYZING", thread_id=thread_id)
        audit(
            "task_start",
            orchestrator="Brain",
            task_id=task_id,
            project_id=project_id,
            description=description[:200],
        )
        state, snapshot = await _stream_brain_events(
            task_id, initial_state, queue, project_id=project_id, lock_holder=lock_holder,
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
        # M-2（对抗复核 Finding A）：调度器停机/失主主动中止——绝不写终态 CANCELLED（假终态会
        # 让任务永久出对账视野=丢在飞工作），保留当前活跃态并 re-raise，交对账恢复重派。
        if is_shutdown_abort(task_id):
            logger.info("[RUNNER] 任务 %s 因调度器停机/失主中止——保留活跃态待对账恢复（不写终态）", task_id)
            raise
        # E4：watchdog 护栏中止也表现为 CancelledError——先查登记，护栏中止走
        # salvage→PARTIAL（与 E5/TokenLimit 同终点）；真人工取消照旧 CANCELLED。
        if await _maybe_salvage_watchdog_abort(task_id, queue):
            pass
        else:
            logger.info("[RUNNER] 任务 %s 已取消", task_id)
            store.update_task(task_id, status="CANCELLED")
            audit("task_cancelled", orchestrator="Brain", task_id=task_id, project_id=project_id)
            await _emit(queue, {
                "step": "cancelled",
                "status": "cancelled",
                "message": "任务已取消",
                "progress": -1,
            })
    except TaskTokenLimitExceeded as _tok_exc:
        # T-B：撞【云端 token 预算】护栏 ≠ 交付失败 → 抢救已完成子任务为 PARTIAL，不整单丢产物。
        # §九 阶段1.4：ledger 阶段闸带 stage 归因（阶段烧穿=该阶段 escalate，消息如实报阶段）。
        _tok_stage = _tok_exc.usage.get("stage")
        logger.warning("[RUNNER] 任务 %s 撞云端 token 预算护栏%s，尝试抢救已完成产物",
                       task_id, f"（阶段={_tok_stage}）" if _tok_stage else "")
        await asyncio.shield(_salvage_partial_from_checkpoint(
            task_id, queue,
            reason_code="token_budget_exceeded",
            reason_msg=(f"云端 token 预算超限 "
                        f"({_tok_exc.usage.get('real_recorded')}/{_tok_exc.usage.get('limit_effective')})"
                        + (f"，阶段 {_tok_stage} 子预算烧穿"
                           f"({_tok_exc.usage.get('stage_spent')}/{_tok_exc.usage.get('stage_limit')})"
                           if _tok_stage else "")),
        ))
    except (TaskWallclockExceeded, TaskLockLost) as _guard_exc:
        # E5（阶段5，登记册 §六）：墙钟/失锁与 TokenLimit 同属【资源护栏中止】——任务
        # 不是"交付失败"而是"被护栏叫停"，已完成子任务是真产物。此前只有 TokenLimit
        # 走 salvage，其余落泛 except 裸 FAILED 整单丢产物（round37 91min 规划烧穿若
        # 叠加墙钟=全丢）。统一 salvage→PARTIAL；checkpoint 取不到时 salvage 内部退回
        # FAILED（不比旧路径差，绝不静默 DONE）。
        _kind = ("wallclock_exceeded" if isinstance(_guard_exc, TaskWallclockExceeded)
                 else "module_lock_lost")
        logger.warning("[RUNNER] 任务 %s 撞资源护栏（%s），尝试抢救已完成产物: %s",
                       task_id, _kind, _guard_exc)
        await asyncio.shield(_salvage_partial_from_checkpoint(
            task_id, queue,
            reason_code=_kind,
            reason_msg=f"资源护栏中止（{_kind}）: {str(_guard_exc)[:200]}",
        ))
    except Exception as exc:
        logger.exception("[RUNNER] 任务 %s 执行失败", task_id)
        # R38-E 复核 F7：泛 except 是 R38-D fail-loud RuntimeError 的必经之路——同样落机读账
        store.update_task(task_id, status="FAILED", error=str(exc)[:300],
                          token_usage=_failed_machine_account(task_id, None, "unhandled_exception"))
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
        audit("task_failed", orchestrator="Brain", task_id=task_id, project_id=project_id, error=str(exc)[:300])
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": f"执行失败: {exc}",
            "progress": -1,
        })
    finally:
        _stop_watchdog(task_id)          # E4：任何退出路径都停看门狗
        _watchdog_abort.pop(task_id, None)
        lock_holder["lock"].release()
        _task_running.discard(task_id)
        # B2：清理 per-task token 归属与真实累计（覆盖正常/超限/异常所有退出路径）。
        try:
            from swarm.models import usage_tracker as _ut
            _ut.set_current_task(None)
            _ut.clear_task_total(task_id)
        except Exception:
            pass
        # §九 阶段1.4：账本段结算（wall_ms）+写穿+出内存（DB 留档，resume 再 attach 恢复）。
        try:
            from swarm.models import ledger as _lg
            _lg.detach(task_id)
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
    # E11（2026-07-09 登记册）：resume 是执行入口，不自我 enqueue（幽灵队列项，见 run_task 同注）。
    from swarm.infra.redis_client import ModuleLock

    _resume_project_id = task.get("project_id", "")
    if _resume_project_id:
        set_task_context(task_id, project_id=_resume_project_id)  # 复核 F6：resume 日志也带 project_id
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
    # D02：锁经可变容器传入 _stream_brain_events，plan 升级锁后原地写回，finally 始终释放【当前】锁。
    lock_holder: dict[str, Any] = {"lock": module_lock}
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
        state, snapshot = await _stream_brain_events(
            task_id,
            Command(resume=resume_payload),
            queue,
            project_id=_resume_project_id,
            lock_holder=lock_holder,
        )
        await _handle_post_run(task_id, state, queue, snapshot)
    except asyncio.CancelledError:
        # M-2（对抗复核 Finding A）：调度器停机/失主中止 → 保留活跃态、绝不写终态、re-raise 交对账。
        if is_shutdown_abort(task_id):
            logger.info("[RUNNER] 任务 %s resume 因调度器停机/失主中止——保留活跃态待对账恢复", task_id)
            raise
        # E4：先查 watchdog 登记——护栏中止走 salvage，人工取消照旧 CANCELLED。
        if await _maybe_salvage_watchdog_abort(task_id, queue):
            return
        # F3：取消是 BaseException，不被 except Exception 捕获——须显式落 CANCELLED，
        # 否则 resume 途中被 cancel_task 取消会把任务卡在 ANALYZING/IN_REVISION（认领已推进的态）
        # 直到重启对账才转终态；与 run_task 的取消处理对齐。
        logger.info("[RUNNER] 任务 %s resume 已取消", task_id)
        store.update_task(task_id, status="CANCELLED")
        await _emit(queue, {
            "step": "cancelled", "status": "cancelled", "message": "任务已取消", "progress": -1,
        })
        raise
    except TaskTokenLimitExceeded as _tok_exc:
        # T-B：resume 途中撞云端 token 预算护栏同样抢救 PARTIAL（与 run_task 对齐）。
        logger.warning("[RUNNER] 任务 %s resume 撞云端 token 预算护栏，尝试抢救已完成产物", task_id)
        await asyncio.shield(_salvage_partial_from_checkpoint(
            task_id, queue,
            reason_code="token_budget_exceeded",
            reason_msg=(f"云端 token 预算超限 "
                        f"({_tok_exc.usage.get('real_recorded')}/{_tok_exc.usage.get('limit_effective')})"),
        ))
    except Exception as exc:
        logger.exception("[RUNNER] 任务 %s resume 失败", task_id)
        store.update_task(task_id, status="FAILED", error=f"resume_failed: {exc}"[:300],
                          token_usage=_failed_machine_account(task_id, None, "resume_failed"))
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": f"恢复失败: {exc}",
            "progress": -1,
        })
    finally:
        _stop_watchdog(task_id)          # E4：任何退出路径都停看门狗
        _watchdog_abort.pop(task_id, None)
        lock_holder["lock"].release()
        _task_running.discard(task_id)
        # 复核 CR-1：resume 也经 _stream_brain_events→set_current_task，必须同样清理 per-task
        # token 归属+累计（否则 resume 后计数残留、retry 时被 max(真实,估算) 误判超限 + 内存泄漏）。
        try:
            from swarm.models import usage_tracker as _ut
            _ut.set_current_task(None)
            _ut.clear_task_total(task_id)
        except Exception:
            pass
        # §九 阶段1.4：resume 段账本结算+写穿+出内存（下次 attach 自 DB 恢复延续）。
        try:
            from swarm.models import ledger as _lg
            _lg.detach(task_id)
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
    # E11（2026-07-09 登记册）：执行入口不自我 enqueue（幽灵队列项，见 run_task 同注）。
    from swarm.infra.redis_client import ModuleLock

    _resume_project_id = task.get("project_id", "")
    if _resume_project_id:
        set_task_context(task_id, project_id=_resume_project_id)  # 复核 F6：resume 日志也带 project_id
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
    # D02：锁经可变容器传入 _stream_brain_events，plan 升级锁后原地写回，finally 始终释放【当前】锁。
    lock_holder: dict[str, Any] = {"lock": module_lock}
    try:
        store.update_task(task_id, status="ANALYZING")
        await _emit(queue, {
            "step": "resume", "status": "running",
            "message": "恢复规划（澄清/方案评审已提交）", "mode": "brain", "progress": 30,
        })
        state, snapshot = await _stream_brain_events(
            task_id,
            Command(resume=payload),
            queue,
            project_id=_resume_project_id,
            lock_holder=lock_holder,
        )
        await _handle_post_run(task_id, state, queue, snapshot)
    except asyncio.CancelledError:
        # M-2（对抗复核 Finding A）：调度器停机/失主中止 → 保留活跃态、绝不写终态、re-raise 交对账。
        if is_shutdown_abort(task_id):
            logger.info("[RUNNER] 任务 %s 规划 resume 因调度器停机/失主中止——保留活跃态待对账恢复", task_id)
            raise
        # E4：先查 watchdog 登记——护栏中止走 salvage，人工取消照旧 CANCELLED。
        if await _maybe_salvage_watchdog_abort(task_id, queue):
            return
        # F3：取消是 BaseException，须显式落 CANCELLED，否则规划 resume 途中被取消会卡在
        # ANALYZING 直到重启对账；与 run_task/resume_task 对齐。
        logger.info("[RUNNER] 任务 %s 规划 resume 已取消", task_id)
        store.update_task(task_id, status="CANCELLED")
        await _emit(queue, {
            "step": "cancelled", "status": "cancelled", "message": "任务已取消", "progress": -1,
        })
        raise
    except TaskTokenLimitExceeded as _tok_exc:
        # T-B：规划 resume 撞云端 token 预算护栏——规划期多无完成子任务 → 抢救逻辑自然落 FAILED，
        # 若已有完成产物（少见）则同样保为 PARTIAL。与 run_task/resume_task 对齐，不走裸 FAILED。
        logger.warning("[RUNNER] 任务 %s 规划 resume 撞云端 token 预算护栏，尝试抢救已完成产物", task_id)
        await asyncio.shield(_salvage_partial_from_checkpoint(
            task_id, queue,
            reason_code="token_budget_exceeded",
            reason_msg=(f"云端 token 预算超限 "
                        f"({_tok_exc.usage.get('real_recorded')}/{_tok_exc.usage.get('limit_effective')})"),
        ))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[RUNNER] 任务 %s 规划 resume 失败", task_id)
        store.update_task(task_id, status="FAILED", error=f"resume_planning_failed: {exc}"[:300],
                          token_usage=_failed_machine_account(task_id, None, "resume_planning_failed"))
        _emit_task_notification(task_id, store.get_task(task_id) or {}, "FAILED")
        await _emit(queue, {"step": "error", "status": "error", "message": f"规划恢复失败: {exc}", "progress": -1})
    finally:
        _stop_watchdog(task_id)          # E4：任何退出路径都停看门狗
        _watchdog_abort.pop(task_id, None)
        lock_holder["lock"].release()
        _task_running.discard(task_id)
        # 复核 CR-1：规划 resume 同样 set_current_task，需清理 per-task token 归属+累计。
        try:
            from swarm.models import usage_tracker as _ut
            _ut.set_current_task(None)
            _ut.clear_task_total(task_id)
        except Exception:
            pass
        # §九 阶段1.4：规划 resume 段账本结算+写穿+出内存。
        try:
            from swarm.models import ledger as _lg
            _lg.detach(task_id)
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


async def reconcile_orphan_tasks(periodic: bool = False) -> dict[str, int]:
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
            # E7（阶段5）：周期模式跳过 SUBMITTED 重入队——队列/调度器本就持有它们，
            # TaskQueue.enqueue 无去重，周期重入队=队列膨胀。启动模式照旧（Redis 可能
            # 被清空，重入队是丢失信号的唯一恢复通道）。
            if periodic:
                stats["skipped_running"] += 1
                continue
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
            # 5.9 复核 #9（HIGH·CONFIRMED）：approve claim→resume 起跑（含 E9 持锁 apply，
            # 可达数秒）窗口内任务 DB=活跃态但尚未进 _task_running——周期对账命中即误杀
            # FAILED+杀沙箱，且 E2 CAS 让后续 resume 的一切状态写被静默拒绝（误杀不可逆）。
            # 宽限窗：periodic 模式下 updated_at 在 2×对账间隔内的活跃任务视为"有心跳"跳过
            # （claim/节点态推进都刷新 updated_at=天然跨进程心跳）；真孤儿（进程死了没人
            # 再写）会超窗被下一轮收走。启动模式不设窗（进程刚重启，活跃态必为孤儿）。
            if periodic:
                import datetime as _dt
                try:
                    _grace_s = float(os.environ.get(
                        "SWARM_RECONCILE_ACTIVE_GRACE_S", "1200") or "1200")
                except ValueError:
                    _grace_s = 1200.0
                _upd = rec.get("updated_at")
                if _upd is not None:
                    if getattr(_upd, "tzinfo", None) is None:
                        _upd = _upd.replace(tzinfo=_dt.timezone.utc)
                    _age = (_dt.datetime.now(_dt.timezone.utc) - _upd).total_seconds()
                    if _age < _grace_s:
                        stats["skipped_running"] += 1
                        continue
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
    logger.info("[RECONCILE] %s对账完成: %s", "周期" if periodic else "启动", stats)
    return stats


# E13（阶段5，登记册 §六）：挂起态 TTL 提醒去重表 {task_id: monotonic 上次提醒时刻}
_ttl_notified: dict[str, float] = {}


async def check_suspended_ttl() -> int:
    """E13：中断挂起态（CONFIRMING/DELIVERING/CLARIFYING/DESIGN_REVIEW）超 TTL →
    升级通知（应用内 notification + loud 日志）。

    不强杀：人工闸挂起是【合法等待人工】，TTL 强 FAIL 会丢真工作——登记册拍板口径
    是"可配 TTL+升级通知"。默认 24h 提醒、每 TTL 周期至多重提醒一次；
    SWARM_INTERRUPT_TTL_NOTIFY_H<=0 关闭。返回本轮提醒条数。"""
    try:
        ttl_h = float(os.environ.get("SWARM_INTERRUPT_TTL_NOTIFY_H", "24") or "24")
    except ValueError:
        ttl_h = 24.0
    if ttl_h <= 0:
        return 0
    ttl_s = ttl_h * 3600.0
    loop = asyncio.get_running_loop()
    try:
        candidates = await loop.run_in_executor(None, store.list_orphan_candidates)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[E13] 读挂起态候选失败，本轮跳过: %s", exc)
        return 0
    import datetime as _dt
    now_dt = _dt.datetime.now(_dt.timezone.utc)
    now_mono = time.monotonic()
    notified = 0
    # 5.9 猎手 F9（LOW）：任务被审批/终态后从候选消失，提醒条目差集清理防慢泄漏。
    _live_ids = {r.get("id") for r in candidates if r.get("id")}
    for _stale in [k for k in _ttl_notified if k not in _live_ids]:
        _ttl_notified.pop(_stale, None)
    for rec in candidates:
        tid = rec.get("id")
        if not tid or rec.get("status") not in _INTERRUPT_SUSPENDED_STATES:
            continue
        upd = rec.get("updated_at")
        if upd is None:
            continue
        if getattr(upd, "tzinfo", None) is None:
            upd = upd.replace(tzinfo=_dt.timezone.utc)
        age_s = (now_dt - upd).total_seconds()
        if age_s < ttl_s:
            _ttl_notified.pop(tid, None)  # 状态有推进（updated_at 刷新）→ 重置提醒
            continue
        _last = _ttl_notified.get(tid)
        if _last is not None and (now_mono - _last) < ttl_s:
            continue  # 每 TTL 周期至多提醒一次
        _ttl_notified[tid] = now_mono
        notified += 1
        logger.warning(
            "[E13] ⚠️ 任务 %s 挂起在 %s 已 %.1fh（超 TTL %.0fh）——等待人工处置："
            "审批/拒绝或 retry；漏配 auto_accept 会无限等待",
            tid, rec.get("status"), age_s / 3600.0, ttl_h)
        try:
            _emit_task_notification(tid, rec, str(rec.get("status")))
        except Exception:  # noqa: BLE001
            pass
    return notified


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
        # E2：retry 是唯一合法的【终态→活跃态】穿越（PARTIAL/DONE/FAILED → SUBMITTED），
        # 显式声明绕过 CAS 终态守卫；其余一切改状态写默认被守卫拒绝（晚到写复活终态）。
        allow_terminal_transition=True,
        retry_prev_thread_id=(task.get("thread_id") or task_id),  # E1：留给 run_task 播种
        plan={},
        merged_diff="",
        subtask_count=0,
        completed_subtasks=0,
        abandoned_subtasks=0,   # D07：retry=全新 thread/清空 plan，放弃计数须归零，否则旧账残留误导进度三本账
        merge_conflicts=[],     # D07：清残留冲突，否则重跑继承旧冲突致 /apply-diff 永久 409（store 用 is not None，[] 生效清空）
        human_decision="",
        thread_id=new_thread_id,
        base_commit="",  # ★B6 复核 #5★：retry=全新 thread/清空 plan → 清 base_commit 令 run_task
                         # 重捕获【当前仓库 HEAD】为新基线（retry 语义=对最新仓库重跑，非沿用旧 birth base）。
    )

    # ★D41 治本★：retry 走 scheduler.submit_task 统一准入——旧口径直跑 run_task 不占
    # _inflight 槽、绕过 MAX_CONCURRENT_TASKS 与项目沙箱就绪闸门（批量重跑=无界并发超卖），
    # 与 reconcile 走 submit_task 的口径分叉。调用方（API retry_task_background）本就
    # fire-and-forget，不依赖同步等待结果，入队语义兼容。调度器消费循环未运行
    # （CLI/测试/未启动）时保留直跑兜底——那些环境本无准入面，入队无人消费会静默丢任务。
    from swarm.brain import scheduler as _scheduler

    if _scheduler.is_consumer_running():
        resolved_auto = auto_accept
        if resolved_auto is None:
            # 与 run_task 对 None 的解析口径一致（env SWARM_AUTO_ACCEPT）
            resolved_auto = os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")
        _scheduler.submit_task(
            task_id,
            task["project_id"],
            task["description"],
            auto_accept=bool(resolved_auto),
            priority=(task.get("queue_priority") or "normal"),
        )
        return True

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

        # M-5（外部深审）：审批恢复走与初始任务【同一 max_concurrent 天花板】——图 interrupt 返回
        # 时已释放调度槽，任务在等人审批期间不占额度；批量审批若直接 create_task 会无界超卖。
        # 此处等到有空位再跑 resume，占位/释放对称（消费器未运行=CLI/测试则不门控）。
        from swarm.brain import scheduler as _sched
        _slotted = False
        with bind_task(task_id):
            try:
                # hunter F2：准入是【优化】不是正确性闸——绝不能因它抛异常把已认领出审批态的
                # 任务卡死（resume_task 才有回滚/SSE 通知的 umbrella）。故 fail-open：等额度失败
                # 就直接跑 resume（宁可短暂过额，不留无错卡死态）。
                try:
                    _slotted = await _sched.await_execution_slot(task_id)
                except Exception as _slot_exc:  # noqa: BLE001
                    logger.warning("[Scheduler] resume 准入等待异常，fail-open 直跑 task=%s: %s",
                                   task_id, _slot_exc)
                    _slotted = False
                await resume_task(task_id, decision, feedback, revert_status=revert_status)
            finally:
                if _slotted:
                    _sched.release_execution_slot(task_id)
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
