"""infra/degrade.py — 降级路径分类计数（E1，进程内轻量 counter）。

系统里 ~200+ `except Exception` 都 log+降级(fail-soft 设计正确)，但无计数 → 生产分不清
【预期降级】(如无网、模型偶发抖动)vs【真 bug】(某降级路径被高频触发)。record_degrade 只
+1 计数(线程安全)、不改任何行为，经 /api/metrics 以 swarm_degrade_total{category} 暴露，
让运维按类别看降级发生频率、设告警阈值。

用法：在降级/兜底分支调 record_degrade("<域>.<路径>")，与既有 logger.warning 并列。
category 用点分层级(如 brain.handle_failure.llm_fallback)便于 Prometheus label 聚合。
"""

from __future__ import annotations

import threading
from collections import defaultdict

_counts: defaultdict[str, int] = defaultdict(int)
_lock = threading.Lock()


def record_degrade(category: str) -> None:
    """给某类降级计数 +1（线程安全，不抛，不改行为）。"""
    if not category:
        category = "unknown"
    with _lock:
        _counts[category] += 1


def degrade_counts() -> dict[str, int]:
    """返回各类别降级累计计数的快照（供 /api/metrics 暴露）。"""
    with _lock:
        return dict(_counts)


def reset_degrade_counts() -> None:
    """清零（测试隔离用）。"""
    with _lock:
        _counts.clear()
