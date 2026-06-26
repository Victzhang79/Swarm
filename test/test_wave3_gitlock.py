#!/usr/bin/env python3
"""Wave 3 并发共享 git 工作树锁（TD2606-B5/C5/M5）。

钉死 _ProjectGitFlock 把同一 project_path 的 git 临界操作串行化：并发 worker 不再在共享
工作树/索引上互踩（add -N 泄漏进对方 diff / reset 与 diff 互踩）。
"""
from __future__ import annotations

import importlib.util
import tempfile
import threading
import time
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_b5_flock_serializes_same_project():
    """同一 root 的两个持锁段必须串行（B 在 A 释放前进不去）。"""
    from swarm.worker.executor import _ProjectGitFlock

    root = tempfile.mkdtemp()
    order: list[str] = []

    def _seg(name: str, hold: float) -> None:
        with _ProjectGitFlock(root):
            order.append(f"{name}-enter")
            time.sleep(hold)
            order.append(f"{name}-exit")

    t1 = threading.Thread(target=_seg, args=("A", 0.2))
    t1.start()
    time.sleep(0.05)  # 确保 A 先拿到锁
    t2 = threading.Thread(target=_seg, args=("B", 0.0))
    t2.start()
    t1.join()
    t2.join()
    assert order == ["A-enter", "A-exit", "B-enter", "B-exit"], f"未串行: {order}"
    print("  ✅ B5：同 project 的 git 临界段串行化")


def test_b5_flock_different_projects_independent():
    """不同 root 的锁互不阻塞（不误伤跨项目并行）。"""
    from swarm.worker.executor import _ProjectGitFlock

    r1, r2 = tempfile.mkdtemp(), tempfile.mkdtemp()
    enters: list[str] = []

    def _seg(root: str, name: str) -> None:
        with _ProjectGitFlock(root):
            enters.append(name)
            time.sleep(0.15)

    t1 = threading.Thread(target=_seg, args=(r1, "A"))
    t2 = threading.Thread(target=_seg, args=(r2, "B"))
    t1.start()
    t2.start()
    time.sleep(0.05)
    # 两个不同项目应都已进入（无相互阻塞）
    assert set(enters) == {"A", "B"}, f"跨项目被误串行: {enters}"
    t1.join()
    t2.join()
    print("  ✅ B5：不同 project 的锁互不阻塞")


def test_b5_flock_degrades_gracefully():
    """构造/锁失败时降级无锁，不抛（with 仍可正常进出）。"""
    from swarm.worker.executor import _ProjectGitFlock

    lock = _ProjectGitFlock("/nonexistent/\x00bad")  # 异常路径 → 内部吞掉、降级
    with lock:
        pass
    print("  ✅ B5：锁不可用时优雅降级无锁")


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {fn.__name__}: {e}")
            fails += 1
    sys.exit(1 if fails else 0)
