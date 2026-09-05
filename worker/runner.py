"""Standalone Worker 运行器 — Phase 0 单 Worker 验证（不经 Brain）

提供 SSE 队列，供 API / UI 直接跑 WorkerExecutor。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from swarm.infra.cancellation import (
    cancel_and_wait,
    cancel_and_wait_all,
    run_blocking_owned,
    run_db_blocking_owned,
)
from swarm.project import store
from swarm.types import FileScope, SubTask, SubTaskDifficulty, WorkerOutput

logger = logging.getLogger(__name__)

_worker_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
_worker_running: set[str] = set()
# A-P1-28：run_id → project_id 映射(进程内，与 _worker_queues 同生命周期)。
# worker 进度流(GET /api/worker/{run_id}/stream)凭此对该项目做成员/权限校验，
# 杜绝"任一已认证用户拿到 run_id 即可读他人 worker 流"的越权。
_worker_run_project: dict[str, str] = {}
# TD2606-B14：run_id → 后台单跑任务句柄。原 start_standalone_worker_background 直接丢弃
# create_task 句柄 → 卡死的单跑既无法外部取消、异常也被静默吞没。在此登记以便取消/可见。
_worker_tasks: dict[str, asyncio.Task] = {}


def _worker_run_queue_max() -> int:
    """M-3：单跑事件队列上界（防无订阅者时无界堆积）。SWARM_WORKER_RUN_QUEUE_MAX 覆盖，默认 2000。"""
    try:
        return max(16, int(os.environ.get("SWARM_WORKER_RUN_QUEUE_MAX", "2000")))
    except (TypeError, ValueError):
        return 2000


def _worker_run_retention_s() -> float:
    """M-3：单跑完成后事件队列/归属映射的保留期（秒），到期回收。给 SSE 订阅者留窗读完终态事件。
    SWARM_WORKER_RUN_RETENTION_S 覆盖，默认 300s。"""
    try:
        v = float(os.environ.get("SWARM_WORKER_RUN_RETENTION_S", "300"))
        return v if v >= 0 else 300.0
    except (TypeError, ValueError):
        return 300.0


def _worker_run_max_retained() -> int:
    """M-3：进程内保留的 run 上界（硬兜底，防保留期内海量单跑撑爆内存）。默认 1000。"""
    try:
        return max(16, int(os.environ.get("SWARM_WORKER_RUN_MAX_RETAINED", "1000")))
    except (TypeError, ValueError):
        return 1000


def cancel_standalone_worker(run_id: str) -> bool:
    """外部取消一个卡死/失控的后台单跑 worker。返回是否实际发起取消。TD2606-B14。"""
    task = _worker_tasks.get(run_id)
    if task is not None and not task.done():
        task.cancel()
        logger.warning("[WORKER] 外部取消后台单跑任务 run_id=%s", run_id)
        return True
    return False


def get_worker_queue(run_id: str) -> asyncio.Queue[dict[str, Any]] | None:
    return _worker_queues.get(run_id)


def register_worker_run_project(run_id: str, project_id: str) -> None:
    """记录 run_id 归属的 project_id(供 stream 端点做所有权校验)。"""
    _worker_run_project[run_id] = project_id


def get_worker_run_project(run_id: str) -> str | None:
    """取 run_id 归属的 project_id；未知 run_id 返回 None。"""
    return _worker_run_project.get(run_id)


def _evict_stale_runs() -> None:
    """M-3：硬兜底——保留的 run 超上界时，按插入序驱逐【已不在跑】的最旧项（queue+项目映射），
    绝不驱逐在跑的 run。保留期 TTL 是常态回收，此处仅防保留期内海量单跑撑爆内存。"""
    cap = _worker_run_max_retained()
    if len(_worker_queues) <= cap:
        return
    for rid in list(_worker_queues.keys()):
        if len(_worker_queues) <= cap:
            break
        if rid in _worker_running:
            continue  # 绝不驱逐在跑的
        _worker_queues.pop(rid, None)
        _worker_run_project.pop(rid, None)


def register_worker_queue(run_id: str) -> asyncio.Queue[dict[str, Any]]:
    # M-3：有界队列（无订阅者时不无界堆积）；_emit 满则丢最旧（保最新，含终态事件）。
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_worker_run_queue_max())
    _worker_queues[run_id] = queue
    _evict_stale_runs()
    return queue


def _cleanup_worker_run(run_id: str) -> None:
    """M-3：单跑完成后（保留期到）回收事件队列 + 归属映射，绝不永久滞留（治"历史结果长期驻留"）。"""
    _worker_queues.pop(run_id, None)
    _worker_run_project.pop(run_id, None)
    _worker_tasks.pop(run_id, None)


def _schedule_worker_run_cleanup(run_id: str) -> None:
    """M-3：单跑收尾后延迟回收——留 _worker_run_retention_s 窗口供 SSE 订阅者读完终态事件再清。"""
    delay = _worker_run_retention_s()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _cleanup_worker_run(run_id)  # 无运行 loop（极少）→ 直接清
        return
    if delay <= 0:
        _cleanup_worker_run(run_id)
    else:
        loop.call_later(delay, _cleanup_worker_run, run_id)


def is_worker_running(run_id: str) -> bool:
    return run_id in _worker_running


async def _emit(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    # M-3：有界队列——满（无订阅者/慢订阅者）则丢【最旧】腾位，绝不阻塞 worker 主流程。丢最旧保
    # 最新（终态 complete/result 恒在队尾被保留），迟到订阅者读到的仍是含终态的尾部事件。
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()  # 丢最旧
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # 极端并发下仍满 → 丢本条（best-effort，不阻塞）


def _standalone_scope(
    writable: list[str] | None,
    readable: list[str] | None,
    create_files: list[str] | None = None,
    delete_files: list[str] | None = None,
) -> FileScope:
    """None/None 表示未声明时的全项目模式；显式空数组保持零授权。"""
    declared = any(
        value is not None
        for value in (writable, readable, create_files, delete_files)
    )
    return FileScope(
        writable=list(writable or []),
        readable=list(readable or []),
        create_files=list(create_files or []),
        delete_files=list(delete_files or []),
        allow_any=not declared,
    )


def _standalone_lock_keys(
    writable: list[str] | None,
    create_files: list[str] | None,
    delete_files: list[str] | None,
    *,
    remote_sandbox: bool,
) -> list[str]:
    """本地执行统一拿项目宽锁；远程编码期才按模块并发。"""
    if not remote_sandbox:
        return ["default"]
    from swarm.infra.redis_client import module_keys_from_plan

    return module_keys_from_plan({
        "subtasks": [{
            "scope": {
                "writable": writable or [],
                "create_files": create_files or [],
                "delete_files": delete_files or [],
            }
        }]
    })


def _standalone_worker_lock_can_narrow(cfg) -> bool:
    return bool(
        cfg.sandbox.use_for_worker
        and cfg.sandbox.api_url
        and not getattr(cfg.sandbox, "allow_local_fallback", False)
    )
def _set_workspace(project_id: str) -> str | None:
    project = store.get_project(project_id)
    if project and project.get("path"):
        # M2 修复：ContextVar 隔离工作根（同步写 os.environ 兼容子进程）
        from swarm.tools.paths import set_workspace_root
        set_workspace_root(project["path"])
        return project["path"]
    return None


async def run_standalone_worker(
    run_id: str,
    project_id: str,
    description: str,
    *,
    difficulty: str = "medium",
    writable: list[str] | None = None,
    readable: list[str] | None = None,
    create_files: list[str] | None = None,
    delete_files: list[str] | None = None,
) -> None:
    """后台执行单 Worker（无 Brain 拆解）。"""
    queue = _worker_queues.get(run_id) or register_worker_queue(run_id)
    if run_id in _worker_running:
        await _emit(queue, {"step": "error", "status": "error", "message": "Worker 已在运行"})
        return

    _worker_running.add(run_id)
    project_path = _set_workspace(project_id)
    if not project_path:
        await _emit(queue, {"step": "error", "status": "error", "message": f"项目不存在: {project_id}"})
        _worker_running.discard(run_id)
        return

    diff_enum = SubTaskDifficulty.MEDIUM
    try:
        diff_enum = SubTaskDifficulty(difficulty.lower())
    except ValueError:
        pass

    # 留空表示独立 Worker 的显式全项目模式；不再用空字符串碰巧匹配所有路径。
    # 只要调用方给出任一清单，就按最小权限解释，另一清单保持空而不是偷偷全开。
    scope = _standalone_scope(writable, readable, create_files, delete_files)
    subtask = SubTask(
        id=run_id,
        description=description,
        difficulty=diff_enum,
        scope=scope,
    )

    from swarm.worker.executor import WorkerExecutor

    executor = WorkerExecutor(
        subtask=subtask,
        scope=scope,
        project_id=project_id,
        project_path=project_path,
    )

    # H-4（外部深审 HIGH）：Standalone Worker 本地模式 agent 直改 project_path、沙箱模式 pull-back
    # 也写回本地树——此前【绝不持模块锁】→ 可与 Brain runner(持模块锁写树)/审批 apply(持 default)/
    # 另一 standalone run 并发写【同一 git 树】=树污染。治：与 Brain 同源，按 writable 派生项目锁
    # （whole-project→default 写者；否则按顶层目录→模块读者），经 H-3 读写门与全体写树者互斥。
    # 拿不到=同项目有任务在写工作树 → fail-loud 让位（非阻塞，绝不静默并发写）。
    from swarm.infra.redis_client import (
        ModuleLock,
        MultiModuleLock,
        RenewPacer,
    )
    from swarm.config.settings import get_config

    cfg = get_config()
    _lock_keys = _standalone_lock_keys(
        writable,
        create_files,
        delete_files,
        remote_sandbox=_standalone_worker_lock_can_narrow(cfg),
    )
    module_lock = (
        ModuleLock(project_id, "default") if _lock_keys == ["default"]
        else MultiModuleLock(project_id, _lock_keys)
    )
    # hunter F2：同步 acquire（与 Brain runner:1359 一致，acquire 非阻塞契约）——绝不用
    # asyncio.to_thread 包：那会在【acquire 已成功但 CancelledError 送达】的窗口里孤儿泄漏锁
    # （内存兜底路径的 threading 锁永不过期=永久泄漏到重启）。
    if not module_lock.acquire():
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": "同项目有任务正在写工作树（模块锁被占用），请稍后重试",
            "mode": "worker",
        })
        _worker_running.discard(run_id)
        return
    log_task: asyncio.Task | None = None
    run_task: asyncio.Task | None = None
    lost_waiter: asyncio.Task | None = None
    try:
        # 锁外 workspace 快照到这里可能被项目删除抢先；拿锁后必须复读持久围栏，
        # 否则 delete 先完成、迟到 standalone 再在已删项目路径写盘。
        locked_project = await run_db_blocking_owned(
            store.get_project,
            project_id,
            operation="standalone 锁内复读项目",
        )
        if (
            not locked_project
            or locked_project.get("status") == "DELETING"
            or locked_project.get("path") != project_path
        ):
            await _emit(queue, {
                "step": "error",
                "status": "error",
                "message": "项目已删除或正在删除，拒绝启动 Worker",
                "mode": "worker",
            })
            return

        _renew_pacer = RenewPacer()
        _lock_lost = asyncio.Event()

        async def _stream_logs() -> None:
            last = 0
            while run_id in _worker_running:
                # H-4：搭车续项目锁 TTL（standalone 可持续数分钟，防长跑 > TTL 静默失锁→他人冒进写树）。
                # hunter F1：renew 返回 False = 确认丢锁（被抢/过期）→ 绝不静默续跑（那正是 H-4 要防的
                # 并发写树）；与 Brain runner:596-613 同源 fail-fast：置事件让主流程中止 executor 写树。
                if _renew_pacer.due(module_lock):
                    if not await run_blocking_owned(
                        module_lock.renew, operation="standalone ModuleLock renew"):
                        await _emit(queue, {
                            "step": "log", "status": "running",
                            "message": "[WARN] 运行期丢失项目锁 → 中止写树（防并发污染）",
                            "phase": executor.phase.value,
                        })
                        _lock_lost.set()
                        return
                logs = executor.execution_log
                if len(logs) > last:
                    for line in logs[last:]:
                        await _emit(queue, {
                            "step": "log",
                            "status": "running",
                            "message": line,
                            "phase": executor.phase.value,
                        })
                    last = len(logs)
                await asyncio.sleep(0.5)

        await _emit(queue, {
            "step": "start",
            "status": "running",
            "message": "Standalone Worker 启动",
            "mode": "worker",
            "project_id": project_id,
        })
        log_task = asyncio.create_task(_stream_logs())
        # hunter F1：executor.run() 与【丢锁事件】赛跑——丢锁先到则取消 run（中止写树）并 fail-loud。
        run_task = asyncio.ensure_future(executor.run())
        lost_waiter = asyncio.ensure_future(_lock_lost.wait())
        await asyncio.wait({run_task, lost_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if _lock_lost.is_set():
            await cancel_and_wait(run_task, operation="丢锁后的 standalone executor")
            raise RuntimeError("standalone worker 运行期丢失项目锁（中止执行，防并发写树）")
        output: WorkerOutput = run_task.result()
        final_status = "done" if output.l1_passed else "failed"
        await _emit(queue, {
            "step": "result",
            "status": final_status,
            "mode": "worker",
            "result": output.model_dump(mode="json"),
        })
        await _emit(queue, {
            "step": "complete",
            "status": final_status,
            "message": "Worker 执行完成" if output.l1_passed else "Worker L1 未通过",
            "mode": "worker",
            "progress": 100,
        })
    except Exception as exc:
        logger.exception("[WORKER] standalone %s failed", run_id)
        await _emit(queue, {
            "step": "error",
            "status": "error",
            "message": str(exc),
            "mode": "worker",
        })
    finally:
        _worker_running.discard(run_id)
        try:
            # 资源所有权硬不变量：三个 child 的 finally（含所有写树线程收尾）全部完成后，
            # 才能释放 ModuleLock。聚合 drain 会延迟传播收尾期间到达的二次取消，避免第一个
            # await 被打断后跳过其余 child。
            await cancel_and_wait_all(tuple(
                (task, operation)
                for task, operation in (
                    (log_task, "standalone 日志流"),
                    (lost_waiter, "standalone 丢锁等待器"),
                    (run_task, "standalone executor"),
                )
                if task is not None
            ))
        finally:
            # H-4：即使聚合 drain 最终重新抛 CancelledError，也必须先释放项目锁。
            try:
                module_lock.release()
            except Exception:  # noqa: BLE001 — 释放失败留痕不掩盖主流程（Redis 路径靠 TTL 兜底）
                logger.warning("[WORKER] standalone %s 释放项目锁失败", run_id, exc_info=True)


def start_standalone_worker_background(
    run_id: str,
    project_id: str,
    description: str,
    **kwargs: Any,
) -> None:
    register_worker_queue(run_id)
    register_worker_run_project(run_id, project_id)  # A-P1-28：记录归属项目供 stream 鉴权
    # TD2606-B14：保留任务句柄 + done-callback——可外部取消，且异常不再被静默吞没。
    task = asyncio.create_task(
        run_standalone_worker(run_id, project_id, description, **kwargs)
    )
    if task is not None:  # 防御：测试可能 mock create_task 返回 None
        _worker_tasks[run_id] = task

        def _on_done(t: asyncio.Task, _rid: str = run_id) -> None:
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error("[WORKER] 后台单跑任务异常退出 run_id=%s: %s", _rid, exc, exc_info=exc)
            # M-3：单跑收尾 → 延迟回收事件队列 + 归属映射 + 句柄（保留期供 SSE 读完终态事件），
            # 绝不永久滞留（治"完成后只清 task handle、queue/project map 长期驻留"的内存泄漏）。
            _schedule_worker_run_cleanup(_rid)

        task.add_done_callback(_on_done)
