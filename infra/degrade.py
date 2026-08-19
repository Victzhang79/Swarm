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


def record_degrade_safe(category: str) -> None:
    """`record_degrade` 的吞异常封装——观测面绝不反噬业务路径。

    ★32 号文 独立双复核 LOW 整改：本函数是**收口**，不是新增第 N 份包装★
    此前全仓有四份同义薄封装各自 `try: from ... import record_degrade`：
    `models/cassette_playback.py` / `brain/contract_utils.py` /
    `brain/nodes/planning_core.py`（`_record_degrade_safe_pc`）/ `api/routers/sandbox.py`。
    它们真正保护的**只有那次延迟 import**（`record_degrade` 自身只加锁自增、文档明写不抛），
    而复核逮到一处真洞：`api/routers/project.py` 在 `except` 臂里做延迟 import 且**不在
    任何 try 内** ⇒ 该 import 若抛，异常会逃出鉴权函数变成 500，恰好破坏"observability
    绝不反噬权限判定"的承诺（fail-closed 该返 False）。
    本模块是叶子（只依赖 threading/collections，无循环依赖风险）⇒ 调用方可**模块级** import
    本函数，连延迟 import 这一步都不需要，洞从形态上消失。
    （另三份既有封装属存量债，收敛它们是独立清理项，本轮不越范围。）
    """
    try:
        record_degrade(category)
    except Exception:  # noqa: BLE001 — 计数失败绝不反噬调用方
        pass


def reset_degrade_counts() -> None:
    """清零（测试隔离用）。"""
    with _lock:
        _counts.clear()
