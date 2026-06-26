#!/usr/bin/env python3
"""TD2606-C4：项目专属镜像(烤源)沙箱不回池——每任务从缓存镜像创建新容器，
杜绝 clean_workspace 抹掉烤进 /workspace 的源码 + 跨任务污染。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _make_executor():
    from swarm.types import FileScope, SubTask, SubTaskDifficulty
    from swarm.worker.executor import WorkerExecutor

    st = SubTask(id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.py"], readable=["a.py"]))
    return WorkerExecutor(subtask=st)


def _release_reusable(*, has_source: bool, l1_passed: bool) -> bool:
    ex = _make_executor()
    pool = MagicMock()
    sb = MagicMock()
    sb.sandbox_id = "sb-1"
    ex._sandbox = sb
    ex._sandbox_manager = MagicMock()
    ex._from_pool = True
    ex._sandbox_pool = pool
    ex._l1_passed_flag = l1_passed
    ex._sandbox_has_source = has_source
    ex.kill_sandbox()
    assert pool.release.called, "应调用 pool.release 归还"
    return pool.release.call_args.kwargs.get("reusable")


def test_c4_baked_sandbox_not_reused():
    # 烤源沙箱（项目专属镜像）即便 L1 通过也【不回池】
    assert _release_reusable(has_source=True, l1_passed=True) is False
    print("  ✅ C4：烤源沙箱 L1 通过仍不回池（reusable=False）")


def test_c4_generic_sandbox_reused_when_clean():
    # 通用模板沙箱 L1 通过 → 正常回池复用
    assert _release_reusable(has_source=False, l1_passed=True) is True
    # 脏沙箱（L1 失败）不回池
    assert _release_reusable(has_source=False, l1_passed=False) is False
    print("  ✅ C4：通用沙箱保持原回池逻辑（仅 L1 通过且非烤源才复用）")


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
