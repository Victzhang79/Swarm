"""取消期间的异步资源所有权协议。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class OwnedBlockingCancelled(asyncio.CancelledError):
    """调用方取消后，被拥有线程最终完成的三态结果。"""

    def __init__(self, state: str, *, result: Any = None, error: BaseException | None = None):
        super().__init__(f"owned blocking call cancelled; child={state}")
        self.state = state
        self.result = result
        self.error = error


async def _drain_task(task: asyncio.Task[Any], *, operation: str) -> bool:
    """等待 task 真正结束，并报告 drain 期间送达调用方的新取消。"""
    caller = asyncio.current_task()
    caller_cancelled = False
    while not task.done():
        cancelling_before = caller.cancelling() if caller is not None else 0
        try:
            # asyncio.wait 不会把等待者取消传播给输入 task，也不会像 shield wrapper
            # 那样在外层已取消、child 随后失败时产生“exception in shielded future”。
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            # shield 既会在【owned child 自己取消】时抛 CancelledError，也会在
            # 【等待者被取消】时抛。后者必须延迟到 child 真结束后向上传播，不能
            # 像旧实现一样留下 cancelling()>0 却正常返回的幽灵状态。
            cancelling_after = caller.cancelling() if caller is not None else 0
            if not task.done() or cancelling_after > cancelling_before:
                caller_cancelled = True
            continue
        except BaseException:  # noqa: BLE001 — 下方统一取 result 留痕
            break
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except BaseException:  # noqa: BLE001 — 收尾异常不得覆盖调用方原控制流
        logger.warning("等待被拥有的 %s 收尾时任务异常", operation, exc_info=True)
    return caller_cancelled


async def run_blocking_owned(
    func: Callable[..., _T],
    /,
    *args: Any,
    operation: str | None = None,
    cancel_result_cleanup: Callable[[_T], Any] | None = None,
    **kwargs: Any,
) -> _T:
    """在线程池运行有副作用的有限阻塞操作，取消时先等操作结束再传播。

    接口不变量：本协程返回或抛出前，``func`` 已不再运行。调用者因此可以安全释放
    保护 ``func`` 副作用的锁或销毁其资源。``func`` 自身必须有有限超时；本函数不会
    为了响应取消而遗弃仍有写能力的线程。
    """
    name = operation or getattr(func, "__qualname__", repr(func))
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        await asyncio.wait((task,))
        return task.result()
    except asyncio.CancelledError:
        await _drain_task(task, operation=name)
        late_state = "cancelled"
        late_result: Any = None
        late_error: BaseException | None = None
        if not task.cancelled():
            try:
                late_result = task.result()
            except BaseException as exc:  # noqa: BLE001 — 通过三态异常交还 owner 判定
                late_state = "error"
                late_error = exc
            else:
                late_state = "success"
        # create/acquire 类调用可能在取消后迟到返回新资源；仅等待线程结束还不够，
        # 因为赋值语句没有执行，调用方 finally 看不到结果。所有权在此显式回收。
        if cancel_result_cleanup is not None and late_state == "success":
            cleanup_task = asyncio.create_task(asyncio.to_thread(
                cancel_result_cleanup, late_result,
            ))
            await _drain_task(cleanup_task, operation=f"{name} 的迟到结果回收")
            try:
                cleanup_task.result()
            except BaseException as exc:  # noqa: BLE001 — 补偿失败必须机读上抛
                late_state = "cleanup_error"
                late_error = exc
        raise OwnedBlockingCancelled(
            late_state, result=late_result, error=late_error
        ) from None


async def run_db_blocking_owned(
    func: Callable[..., _T],
    /,
    *args: Any,
    operation: str | None = None,
    db_timeout_s: float | None = None,
    cancel_result_cleanup: Callable[[_T], Any] | None = None,
    **kwargs: Any,
) -> _T:
    """运行 scoped 有界 DB 调用；取消时仍等待服务端超时令线程真正结束。"""
    name = operation or getattr(func, "__qualname__", repr(func))

    def _bounded_call() -> _T:
        from swarm.infra.db import owned_db_timeout

        with owned_db_timeout(db_timeout_s):
            return func(*args, **kwargs)

    def _bounded_cleanup(result: _T) -> Any:
        if cancel_result_cleanup is None:
            return None
        from swarm.infra.db import owned_db_timeout

        with owned_db_timeout(db_timeout_s):
            return cancel_result_cleanup(result)

    return await run_blocking_owned(
        _bounded_call,
        operation=name,
        cancel_result_cleanup=_bounded_cleanup if cancel_result_cleanup else None,
    )


async def cancel_and_wait(task: asyncio.Task[Any], *, operation: str) -> None:
    """取消一个被当前调用方拥有的异步 task，并等待其 finally 完成。"""
    await cancel_and_wait_all(((task, operation),))


async def drain_owned_task(task: asyncio.Task[Any], *, operation: str) -> None:
    """等待 owned task 收尾；期间的新取消延迟到收尾后原样传播。"""
    if await _drain_task(task, operation=operation):
        raise asyncio.CancelledError


async def cancel_and_wait_all(
    owned_tasks: Iterable[tuple[asyncio.Task[Any], str]],
) -> None:
    """取消并收齐一组 owned task；期间送达的取消在全部收尾后传播。

    调用方若还拥有同步资源，应以 ``try/finally`` 包住本函数并在 finally 释放；
    这样二次取消既不会跳过其他 child，也不会跳过锁释放。
    """
    entries = tuple(owned_tasks)
    for task, _operation in entries:
        if not task.done():
            task.cancel()

    caller_cancelled = False
    for task, operation in entries:
        caller_cancelled = (
            await _drain_task(task, operation=operation)
            or caller_cancelled
        )
    if caller_cancelled:
        raise asyncio.CancelledError
