#!/usr/bin/env python3
"""P7：fix 循环【时间维度】提前 bail 阈值（996db614 实测 18×900s grind 兜底）。

no-progress 早停按单轮签名判，模型每轮把错改一点点→签名变→不早停→烧满 900s×多次重试。
加时间维度：已用预算超阈值 + 确定性闸门仍红 + ≥1 轮 LLM 修复仍没过 → 提前 bail。
本测钉死阈值计算（占比 + 钳位 + env 覆盖 + 容错）。
"""
from __future__ import annotations

import os

from swarm.types import FileScope, SubTask, SubTaskDifficulty
from swarm.worker.executor import WorkerExecutor


def _w():
    st = SubTask(id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.java"]))
    return WorkerExecutor(st, task_id="t1")


def _set(v):
    if v is None:
        os.environ.pop("SWARM_WORKER_VERIFY_BAIL_FRACTION", None)
    else:
        os.environ["SWARM_WORKER_VERIFY_BAIL_FRACTION"] = v


def test_default_fraction_0_6():
    old = os.environ.get("SWARM_WORKER_VERIFY_BAIL_FRACTION")
    _set(None)
    try:
        w = _w()
        assert abs(w._verify_bail_seconds() - 0.6 * w.max_execution_time) < 1e-6
    finally:
        _set(old)


def test_env_override():
    old = os.environ.get("SWARM_WORKER_VERIFY_BAIL_FRACTION")
    _set("0.8")
    try:
        w = _w()
        assert abs(w._verify_bail_seconds() - 0.8 * w.max_execution_time) < 1e-6
    finally:
        _set(old)


def test_clamp_low_and_high():
    old = os.environ.get("SWARM_WORKER_VERIFY_BAIL_FRACTION")
    try:
        _set("0.05")  # 太低 → 钳到 0.3
        w = _w()
        assert abs(w._verify_bail_seconds() - 0.3 * w.max_execution_time) < 1e-6
        _set("5.0")   # 太高 → 钳到 0.95
        w = _w()
        assert abs(w._verify_bail_seconds() - 0.95 * w.max_execution_time) < 1e-6
    finally:
        _set(old)


def test_garbage_env_falls_back_0_6():
    old = os.environ.get("SWARM_WORKER_VERIFY_BAIL_FRACTION")
    _set("abc")
    try:
        w = _w()
        assert abs(w._verify_bail_seconds() - 0.6 * w.max_execution_time) < 1e-6
    finally:
        _set(old)


def test_bail_less_than_full_budget():
    # 核心性质：bail 阈值严格小于总预算 → 不会烧满到超时
    old = os.environ.get("SWARM_WORKER_VERIFY_BAIL_FRACTION")
    _set(None)
    try:
        w = _w()
        assert w._verify_bail_seconds() < w.max_execution_time
    finally:
        _set(old)


if __name__ == "__main__":
    import sys
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v()
            except Exception as e:  # noqa: BLE001
                import traceback
                print(f"  ❌ {k}: {e}")
                traceback.print_exc()
                fails += 1
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
