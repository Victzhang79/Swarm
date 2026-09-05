"""不可信 Worker 同步 Tool 的跨线程 in-flight 生命周期租约。"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager


class ToolInflightTracker:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._count = 0
        self._accepting = True
        self._local_deletions: set[str] = set()

    @contextmanager
    def lease(self):
        with self._condition:
            if not self._accepting:
                raise RuntimeError(
                    "[WORKER_TOOL_LIFECYCLE_CLOSED] Worker 已进入收尾，拒绝迟到副作用"
                )
            self._count += 1
        try:
            yield
        finally:
            with self._condition:
                self._count -= 1
                if self._count == 0:
                    self._condition.notify_all()

    def close_and_wait(self) -> None:
        with self._condition:
            self._accepting = False
            while self._count:
                self._condition.wait()

    def record_local_deletion(self, rel: str) -> None:
        """在线程租约内登记已经落到宿主工作树的声明删除。"""
        with self._condition:
            self._local_deletions.add(str(rel))

    def consume_local_deletions(self) -> set[str]:
        """原子取走尚未结算的本地删除，避免回滚后被累计账重新加入。"""
        with self._condition:
            deletions = set(self._local_deletions)
            self._local_deletions.clear()
            return deletions


_tracker_var: contextvars.ContextVar[ToolInflightTracker | None] = (
    contextvars.ContextVar("swarm_tool_inflight_tracker", default=None)
)


def set_tool_inflight_tracker() -> ToolInflightTracker:
    tracker = ToolInflightTracker()
    _tracker_var.set(tracker)
    return tracker


def clear_tool_inflight_tracker() -> None:
    _tracker_var.set(None)


def current_tool_inflight_tracker() -> ToolInflightTracker | None:
    """返回当前 Worker 生命周期的精确 tracker。

    调用方必须传回这个实例做 close-and-drain，不得在收尾时新建
    tracker，否则已复制 ContextVar 的工具线程仍持有旧门禁。
    """
    return _tracker_var.get()


@contextmanager
def track_tool_side_effect():
    tracker = _tracker_var.get()
    if tracker is None:
        yield
        return
    with tracker.lease():
        yield


def record_local_tool_deletion(rel: str) -> None:
    """把本地 delete_file 的已提交副作用写入当前 Worker 生命周期账。"""
    tracker = _tracker_var.get()
    if tracker is not None:
        tracker.record_local_deletion(rel)


def close_and_wait_for_tool_side_effects(tracker: ToolInflightTracker) -> None:
    """原子关门后排空；关门前尚未取得 lease 的迟到线程将被拒绝。"""
    tracker.close_and_wait()
