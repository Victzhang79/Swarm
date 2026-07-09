"""任务准入调度器 — 让优先级队列真正生效（进程内有界并发）。

设计说明（架构决策）:
    当前 Brain 在 API 进程内通过 asyncio 执行（非独立 worker 进程）。本调度器
    在此模型下引入"准入控制"：create_task 不再直接 fire Brain，而是入优先级队列，
    由后台消费循环按 max_concurrent 上限取任务执行。

    效果:
    - urgent > normal > background 优先级真正生效（高优先级任务插队）
    - 全局并发上限（SWARM_MAX_CONCURRENT_TASKS，默认取 worker.max_concurrent）
    - 队列积压可观测（pending_count）

    边界:
    - 仍是单进程模型。多 API 副本需要外置队列（见 README Roadmap）。
    - resume（审核后恢复）不走队列，直接执行（已在审核态，不占新并发额度）。

    ── round24 B2：多副本协调 = WONTFIX（当前部署=内网多用户多项目【单进程】）──
    目标部署为单进程，下列进程内全局态在单进程下【正确】（内存即全局事实源），故本轮不搬
    到 Redis/PG，标 WONTFIX 并留【多副本迁移清单】备将来水平扩容时按此逐项外置：
      1. brain/runner：_task_running / _task_queues / _task_handles（在飞/队列/句柄）
      2. brain/scheduler：_inflight / _pending_meta（本模块并发计数与待跑元数据）
      3. api 限流器 _limiter（令牌桶）· 登录节流 _LoginThrottle（失败锁定）
      4. brain/graph：编译图 / checkpointer 单例
    迁移事实源统一走 Redis/PG；跨进程任务认领的现成入口是 scheduler.is_task_claimed
    （已有 Redis SET NX 语义，B1 的 ModuleLock 亦然）。resume 绕过并发上限在单进程可控；
    多副本下需一并纳入外置准入。启用多副本前：模块锁必启 Redis（否则仅进程内互斥，见 B1）。
"""

from __future__ import annotations

import asyncio
import logging
import os

from swarm.config.settings import get_config
from swarm.infra.redis_client import TaskQueue

logger = logging.getLogger(__name__)

# 任务描述缓存：task_id → (project_id, description, auto_accept)
# 队列只存 task_id/project_id，执行参数在此补全（避免 Redis payload 过大）
_pending_meta: dict[str, dict] = {}

# 准入闸门重试计数：task_id → 因沙箱未就绪被留池的次数（防 ERROR 项目无限 re-enqueue）
_admission_retries: dict[str, int] = {}
# 超过此次数仍未就绪 → 放弃留池，强制放行交由 runner 兜底（executor 选通用池模板）
_MAX_ADMISSION_RETRIES = 200  # ×3s ≈ 10min，覆盖最长沙箱构建耗时
# D58：准入等待改【按任务记 next-retry】——旧实现留池后全局 sleep(3.0) 在消费循环内，
# 一个项目沙箱未就绪会把整条队列（其它就绪项目的任务）一起卡 3s（队头阻塞）。
# task_id → monotonic 时刻，未到期的留池任务出队即回队尾、不做就绪检查也不计重试。
_admission_next_retry: dict[str, float] = {}
_ADMISSION_RETRY_DELAY_S = 3.0  # 与旧 sleep(3.0) 同节奏：同一任务两次就绪检查间隔 ≥3s
# 防热旋：一轮内第二次见到同一未到期任务 = 队列里只剩等待项 → 短睡让出循环
_deferred_cycle: set[str] = set()

_consumer_started = False
_inflight: set[str] = set()
_wakeup: asyncio.Event | None = None
# N-09：持引用保存消费循环 task，供 stop_task_scheduler 取消（否则 fire-and-forget
# 可被 GC、且无法停止）。
_consumer_task: asyncio.Task | None = None


def _max_concurrent() -> int:
    raw = os.environ.get("SWARM_MAX_CONCURRENT_TASKS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, get_config().worker.max_concurrent)


def submit_task(
    task_id: str,
    project_id: str,
    description: str,
    *,
    auto_accept: bool = False,
    priority: str = "normal",
) -> None:
    """提交任务到优先级队列（准入控制，不立即执行）。"""
    _pending_meta[task_id] = {
        "project_id": project_id,
        "description": description,
        "auto_accept": auto_accept,
    }
    TaskQueue.enqueue(task_id, project_id, priority=priority)
    logger.info("[Scheduler] 任务入队 task=%s priority=%s", task_id, priority)
    if _wakeup is not None:
        _wakeup.set()


def pending_count() -> int:
    """当前队列积压 + 在执行任务数（监控用）。"""
    return len(_pending_meta) + len(_inflight)


def is_task_claimed(task_id: str) -> bool:
    """task 是否已被本进程认领：在调度器并发槽（_inflight，已出队待/在跑）或 runner 在跑。

    供启动对账（reconcile）用：仅查 runner._task_running 会漏掉"已出队进 _inflight 但
    run_task 尚未把 task 加进 _task_running"的窗口 → 误判孤儿重入队（虽被本函数在 dequeue
    侧兜住不会双跑，但会产生虚假恢复日志 + 冗余 Redis 项）。
    """
    from swarm.brain.runner import is_task_running

    return task_id in _inflight or is_task_running(task_id)


# 去重守卫（dequeue 侧）：语义同 is_task_claimed，保留旧名供 _loop 引用。
_is_already_running = is_task_claimed


def is_consumer_running() -> bool:
    """D41：调度器消费循环是否在跑。retry_task 据此决定走统一准入（submit_task 入队）
    还是直跑兜底（CLI/测试等无调度器环境，入队无人消费会静默丢任务）。"""
    return _consumer_task is not None and not _consumer_task.done()


def _resolve_exec_meta(task_id: str) -> dict | None:
    """取任务执行 meta：进程内 _pending_meta 补全参数，DB status 复核准入（P0-A + D40）。

    返回 None = 该队列项应丢弃（DB 无记录 / 非 SUBMITTED / 状态复核失败——fail-closed）。
    重建成功会回填 _pending_meta，避免同任务后续再查库。
    """
    meta = _pending_meta.get(task_id)
    from swarm.project import store

    # fail-closed：队列的唯一合法待跑项是 **SUBMITTED**（已入队、尚未开跑、无 checkpoint）。
    # ★对抗复核 P0 治本★：此前放行整个 ACTIVE_EXECUTION_STATES → 冷启动时 Redis 残留队列项
    # 若指向一个【已开跑/审批认领后为 ANALYZING】的任务，会被凭空用【全新 initial_state】在同
    # thread_id 上再跑一遍 run_task（非 Command(resume)）→ 与 PG checkpoint 里挂起的 interrupt/
    # 半执行图状态双写互踩。故只认 SUBMITTED；任何非 SUBMITTED 的队列项一律丢弃（交对账处置）。
    # ★D40 治本★：状态复核对【缓存命中】路径同样生效——排队期任务可能被 cancel/终态化
    # （DB 已 CANCELLED 而缓存 meta 仍在），旧口径缓存命中直接放行 → 已取消任务照常 run_task。
    # DB 读失败也拒绝本次出队（任务仍是 SUBMITTED，自愈排水/重启对账会补），绝不盲放。
    try:
        rec = store.get_task(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Scheduler] 任务 %s 出队状态复核读库失败，fail-closed 丢弃本次出队（排水会补）: %s",
            task_id, exc,
        )
        return None
    if rec is None or rec.get("status") != "SUBMITTED":
        # 终态/取消/已开跑任务的陈旧 meta 一并清理，不泄漏
        _pending_meta.pop(task_id, None)
        return None
    if meta is not None:
        return meta
    meta = {
        "project_id": rec["project_id"],
        "description": rec["description"],
        "auto_accept": bool(rec.get("auto_accept", False)),
    }
    _pending_meta[task_id] = meta
    logger.info("[Scheduler] 任务 %s 从 DB 重建执行 meta（重启恢复）", task_id)
    return meta


# ── 2nd#3：自愈排水 —— DB 权威源，队列丢失不必等重启对账 ────────────────
_last_drain_ts: float = 0.0
_DRAIN_INTERVAL_S = 30.0  # idle 节流：队列持续空时最多每 30s 查一次 DB 补漏


async def _drain_stranded_submitted() -> int:
    """DB 里 status=SUBMITTED 但既不在飞(_inflight)也不在跑、且此刻队列已空 → 判为【陈滞项】
    （Redis 后端切换/flap 丢了队列项，或内存队列被清），重入队。

    使 DB 成为权威源、TaskQueue 退化为【自修复的派生缓存】——不必等下次重启的
    reconcile_orphan_tasks。仅在【队列空 + 有空槽】的 idle tick 调用（见 _loop），故此刻
    SUBMITTED-not-inflight 必为陈滞（真排队项会让队列非空），无误重入队。fail-closed：只认
    SUBMITTED（与 _resolve_exec_meta 同口径，非 SUBMITTED 交对账/resume 处置，不凭空双跑）。

    对抗复核修正：①DB 查询走 run_in_executor 不堵事件循环(F3)；②逐条 try/except——Redis flap
    中途 enqueue 抛错不弃其余陈滞项(F2)；③不回填 _pending_meta——出队时 _resolve_exec_meta 从 DB
    重建，免 drained-then-cancelled 的 meta 泄漏(F6)；④有恢复则 _wakeup.set() 立即消费(F4)。"""
    from swarm.project import store

    loop = asyncio.get_running_loop()
    try:
        cands = await loop.run_in_executor(None, store.list_orphan_candidates)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Scheduler] 自愈排水查询失败(非致命): %s", exc)
        return 0
    n = 0
    for rec in cands:
        if rec.get("status") != "SUBMITTED":
            continue
        tid = rec["id"]
        if _is_already_running(tid):
            continue
        try:
            TaskQueue.enqueue(tid, rec["project_id"], priority=rec.get("queue_priority") or "normal")
            n += 1
        except Exception as exc:  # noqa: BLE001 — 单条失败(Redis flap)不弃其余陈滞项
            logger.warning("[Scheduler] 自愈排水：任务 %s 重入队失败(跳过,下轮再试): %s", tid, exc)
    if n:
        logger.info("[Scheduler] 自愈排水：重入队 %d 个陈滞 SUBMITTED 任务（队列丢失/Redis flap 恢复）", n)
        if _wakeup is not None:
            _wakeup.set()  # 唤醒消费循环立即取，不等 2s 轮询
    return n


def queue_stats() -> dict[str, int]:
    """P2-D：调度器可观测快照——在飞并发数、进程内待跑 meta 数、并发上限。"""
    return {
        "inflight": len(_inflight),
        "pending_meta": len(_pending_meta),
        "max_concurrent": _max_concurrent(),
    }


async def _maybe_drain_stranded() -> None:
    """节流包装：队列持续空时按 _DRAIN_INTERVAL_S 触发排水，避免每个 idle tick 查库。"""
    global _last_drain_ts
    import time as _time

    now = _time.monotonic()
    if now - _last_drain_ts < _DRAIN_INTERVAL_S:
        return
    _last_drain_ts = now
    await _drain_stranded_submitted()


def _project_ready_for_exec(project_id: str) -> bool:
    """准入闸门：项目是否可以启动任务执行。

    构建期间任务仅入池不启动（docs/DESIGN_project_sandbox_prebake_source.md §5.1）。
    判据：项目 status == READY。预处理流水线中 _phase_build_sandbox（Phase 5）在 READY
    之前执行——专属沙箱构建成功（写入 sandbox_template）或明确回退通用池后，项目才置 READY。
    所以 status==READY 即意味着"沙箱已就绪（专属模板 or 通用池兜底）"，可放行。
    PREPROCESSING / BUILDING / ERROR / 不存在 → 不放行（留池等待；ERROR 由上层清理）。

    读项目失败时保守放行（避免因 DB 抖动卡死所有任务；执行端 executor 仍会兜底选模板）。
    """
    return _project_exec_admission(project_id) != "wait"


def _project_exec_admission(project_id: str) -> str:
    """E12（阶段5，登记册 §六）：三态准入——"ready"放行 / "wait"留池 / "error" fail-fast。

    旧口径 ERROR 项目与 BUILDING 同判"留池"，200 次×3s 重试后【强制放行】——预处理
    已明确失败的项目根本没有可用沙箱/索引，放行=注定失败的执行白烧 10 分钟等待+一整轮
    worker。ERROR → 直接 fail-fast（调用侧标任务 FAILED，可 retry 等项目修复后重跑）。
    读项目失败/记录缺失保守 "ready"（原语义：DB 抖动不卡死队列，executor 兜底）。"""
    try:
        from swarm.project.store import get_project
        proj = get_project(project_id)
        if not proj:
            return "ready"  # 项目记录缺失，保守放行交由 runner 处理
        status = (proj.get("status") or "").upper()
        if status == "READY":
            return "ready"
        if status == "ERROR":
            return "error"
        return "wait"
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Scheduler] 准入检查读项目失败，保守放行 %s: %s", project_id, exc)
        return "ready"


async def start_task_scheduler() -> None:
    """启动后台消费循环（API startup 调用，幂等）。"""
    global _consumer_started, _wakeup, _last_drain_ts
    if _consumer_started:
        return
    _consumer_started = True
    _wakeup = asyncio.Event()
    # F5：首个排水延后一个间隔，避开与启动期 reconcile_orphan_tasks 撞车重复入队（都无害去重，但省churn）。
    import time as _time
    _last_drain_ts = _time.monotonic()

    async def _loop() -> None:
        from swarm.brain.runner import start_task_background

        # N-09：每次迭代包 try/except 保活——单次出队/准入/派发异常绝不能杀死整个消费
        # 循环（否则后续所有任务永久卡队列且无告警）。CancelledError 须放行以支持优雅停止。
        import time as _t

        while True:
            try:
                # D58：Redis 模式下 BLPOP 已充当"等待"（enqueue 即刻唤醒）；本轮是否已在
                # 出队处阻塞等待过，决定尾部是否还需要 _wakeup 轮询等待。
                _waited_in_dequeue = False
                # 并发未满则尝试出队
                if len(_inflight) < _max_concurrent():
                    if TaskQueue.supports_blocking():
                        # D58：BLPOP 三 key 一次往返、队列空时事件化等待 ≤2s（替代每 2s
                        # tick 3 个 LPOP 轮询）。阻塞发生在 Redis 连接上，故必须卸线程——
                        # 事件循环保持存活；2s 上限保证 stop/失主停调度器及时生效。
                        item = await asyncio.to_thread(TaskQueue.dequeue_blocking, 2.0)
                        _waited_in_dequeue = True
                    else:
                        item = TaskQueue.dequeue()
                    if item:
                        task_id = item["task_id"]
                        # 去重守卫：同 task 已在跑/在飞（重入队 or 重启后 Redis 残留双份）→
                        # 丢弃本次出队，绝不双跑（否则同任务两条执行链烧资源、状态互踩）。
                        if _is_already_running(task_id):
                            logger.info("[Scheduler] 任务 %s 已在执行，丢弃重复出队项", task_id)
                            continue
                        # P0-A：进程内 meta 缺失（leader 重启，Redis 队列存活但 _pending_meta 清零）
                        # → 从 DB 重建（不再静默丢）；返回 None 表示陈旧项（无记录/已终态）应丢弃。
                        # E8（阶段5）：同步 PG 查询卸线程——DB 慢时不再冻结整个事件循环
                        meta = await asyncio.to_thread(_resolve_exec_meta, task_id)
                        if meta is None:
                            logger.info("[Scheduler] 任务 %s 无有效记录或已终态，丢弃陈旧队列项", task_id)
                            continue
                        # D58：留池任务未到 next-retry → 直接回队尾（不做就绪检查、不计重试、
                        # 不 sleep），后队的就绪任务照常流动（去队头阻塞）。
                        _nr = _admission_next_retry.get(task_id)
                        if _nr is not None and _t.monotonic() < _nr:
                            TaskQueue.enqueue(task_id, meta["project_id"],
                                              priority=item.get("priority", "normal"))
                            if task_id in _deferred_cycle:
                                # 一轮内第二次遇到同一未到期任务 → 队列里只剩等待项，短睡防热旋
                                _deferred_cycle.clear()
                                await asyncio.sleep(0.5)
                            else:
                                _deferred_cycle.add(task_id)
                            continue
                        _deferred_cycle.discard(task_id)
                        # ── 准入闸门：项目专属沙箱未就绪 → 任务留池等待，不启动执行 ──
                        # （docs/DESIGN_project_sandbox_prebake_source.md §5.1：构建期间任务仅入池）
                        _adm = await asyncio.to_thread(
                            _project_exec_admission, meta["project_id"])  # E8：卸线程
                        if _adm == "error":
                            # E12：项目预处理已 ERROR——留池/强制放行都是白烧，fail-fast
                            logger.warning(
                                "[Scheduler] 任务 %s 所属项目 %s 处于 ERROR（预处理失败）→ "
                                "任务 fail-fast 标 FAILED（修复项目后可 retry）",
                                task_id, meta["project_id"])
                            try:
                                from swarm.audit import audit as _audit
                                from swarm.brain.runner import (
                                    _emit_task_notification as _notify,
                                )
                                from swarm.project import store as _store
                                await asyncio.to_thread(
                                    _store.update_task, task_id, status="FAILED")
                                # 5.9 猎手 F8：其余 FAILED 路径都有通知+审计——这条不能静默
                                _rec = await asyncio.to_thread(_store.get_task, task_id) or {}
                                _notify(task_id, _rec, "FAILED")
                                _audit("task_failed", orchestrator="Scheduler",
                                       task_id=task_id, project_id=meta["project_id"],
                                       error="project_error_failfast")
                            except Exception as _exc:  # noqa: BLE001
                                logger.warning("[Scheduler] ERROR 项目任务标 FAILED 失败: %s", _exc)
                            _admission_retries.pop(task_id, None)
                            _admission_next_retry.pop(task_id, None)
                            _pending_meta.pop(task_id, None)
                            continue
                        if _adm != "ready":
                            n = _admission_retries.get(task_id, 0) + 1
                            _admission_retries[task_id] = n
                            if n <= _MAX_ADMISSION_RETRIES:
                                # 重新入队尾部，稍后再试（不消费 meta），避免忙等。
                                # P2 修复优先级反转：保留任务【原优先级】，不要硬降为 normal——
                                # 否则 urgent 任务因沙箱未就绪留池一次即被降级，会被后到的
                                # normal/background 抢先出队执行（高优先级被饿死）。
                                orig_priority = item.get("priority", "normal")
                                TaskQueue.enqueue(task_id, meta["project_id"], priority=orig_priority)
                                # D58：全局 sleep(3.0) 改按任务 next-retry——同一任务的就绪检查
                                # 节奏不变（≥3s 一次），但不再把整条队列卡住 3s。
                                _admission_next_retry[task_id] = _t.monotonic() + _ADMISSION_RETRY_DELAY_S
                                if n == 1 or n % 20 == 0:
                                    logger.info("[Scheduler] 任务 %s 等待项目 %s 沙箱就绪，留池中（第 %d 次）",
                                                task_id, meta["project_id"], n)
                                continue
                            # 超上限：放弃留池，强制放行交由 runner/executor 兜底（通用池模板）
                            logger.warning("[Scheduler] 任务 %s 等待沙箱就绪超 %d 次，强制放行（executor 兜底选模板）",
                                           task_id, _MAX_ADMISSION_RETRIES)
                        _admission_retries.pop(task_id, None)
                        _admission_next_retry.pop(task_id, None)
                        _deferred_cycle.clear()  # 有任务真正派发 = 新一轮，等待项重新计
                        _pending_meta.pop(task_id, None)
                        _inflight.add(task_id)
                        _run_with_slot(task_id, meta, start_task_background)
                        continue  # 立即尝试下一个（填满并发额度）
                    # 队列空 + 有空槽 → 2nd#3 自愈排水（节流）：DB 里 SUBMITTED 但队列已丢的陈滞项
                    # 重入队，不必等下次重启对账（Redis flap/内存队列清 后自修复）。
                    _deferred_cycle.clear()  # 队列已空 = 一轮结束
                    await _maybe_drain_stranded()
                # ★复核 Item 3★：持续满负载下队列【永不空】→ 上面的排水分支永不触达 → 队列已丢的陈滞
                # SUBMITTED 任务永久静默卡死(无日志/无告警)。故【无条件】再跑一次节流排水(30s 内幂等)——
                # 去重守卫(_is_already_running)令重入队合法在队项无害(至多一次多余出队),代价可忽略。
                await _maybe_drain_stranded()
                # 队列空或并发已满 → 等唤醒或轮询。D58：BLPOP 路径本轮已在出队处等待过 2s，
                # 不再叠加 _wakeup 等待（否则空闲延迟翻倍且 BLPOP 之外的窗口听不到唤醒）。
                if not _waited_in_dequeue:
                    try:
                        if _wakeup is not None:
                            await asyncio.wait_for(_wakeup.wait(), timeout=2.0)
                            _wakeup.clear()
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                logger.info("[Scheduler] 消费循环被取消，退出")
                raise
            except Exception as exc:  # noqa: BLE001 — 保活：任何单次异常都不得杀死循环
                logger.exception("[Scheduler] 消费循环单次迭代异常（已保活，继续）: %s", exc)
                await asyncio.sleep(1.0)  # 防热旋

    global _consumer_task
    _consumer_task = asyncio.create_task(_loop())
    logger.info("[Scheduler] 任务准入调度器已启动 (max_concurrent=%d)", _max_concurrent())


async def stop_task_scheduler() -> None:
    """停止后台消费循环（应用关闭调用，幂等）。N-09：取消并清理状态以便重启。"""
    global _consumer_started, _consumer_task, _wakeup
    if _consumer_task is not None and not _consumer_task.done():
        _consumer_task.cancel()
        try:
            await _consumer_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _consumer_task = None
    _consumer_started = False
    _wakeup = None


def _run_with_slot(task_id: str, meta: dict, start_fn) -> None:
    """执行任务并在完成时释放并发额度。"""
    import asyncio as _asyncio

    async def _wrap() -> None:
        from swarm.logging_config import bind_task

        with bind_task(task_id):
            try:
                # start_task_background 内部 create_task 异步执行；这里要等它真正跑完
                # 才能释放额度，所以直接 await run_task 逻辑的包装。
                from swarm.brain.runner import run_task

                await run_task(
                    task_id,
                    meta["project_id"],
                    meta["description"],
                    auto_accept=meta["auto_accept"],
                )
            except _asyncio.CancelledError:
                # 取消时静默退出（cancel_task 已负责 DB 状态 + 沙箱释放）
                logger.info("[Scheduler] 任务 %s 被取消", task_id)
                raise
            except Exception as exc:
                logger.exception("[Scheduler] 任务执行异常 task=%s: %s", task_id, exc)
            finally:
                _inflight.discard(task_id)
                # 仅当当前 handle 仍是自己时才清理，避免误删重跑产生的新 handle
                if _task_handles.get(task_id) is _current_task():
                    _task_handles.pop(task_id, None)
                if _wakeup is not None:
                    _wakeup.set()  # 释放额度 → 唤醒消费循环取下一个

    # 注册 SSE 队列（与原 start_task_background 行为一致）
    from swarm.brain.runner import _task_handles, register_task_queue

    register_task_queue(task_id)
    task_obj = _asyncio.create_task(_wrap())
    # 关键：把 handle 注册到 _task_handles，使 cancel_task 能 handle.cancel() 真正中断
    # （否则取消只翻 DB 状态，asyncio 任务与 LLM 调用继续跑，小模型资源不释放）。
    _task_handles[task_id] = task_obj


def _current_task():
    import asyncio as _asyncio

    try:
        return _asyncio.current_task()
    except RuntimeError:
        return None
