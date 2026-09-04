"""scheduler/run_task 共用的初始执行 epoch 准入判据。"""

from __future__ import annotations

from typing import Any, Mapping


def is_runnable_execution_epoch(record: Mapping[str, Any] | None) -> bool:
    """仅放行普通 SUBMITTED 或协议明确的 v1 execute/retry claim。"""
    if not record or record.get("status") != "SUBMITTED":
        return False
    saga = record.get("resume_saga") or {}
    if not saga:
        return True
    version = saga.get("version")
    if type(version) is not int or version != 1:
        return False
    if not saga.get("saga_id"):
        return False
    return (saga.get("kind"), saga.get("phase")) in {
        ("execute_claim", "claimed"),
        ("retry_claim", "submitted"),
    }
