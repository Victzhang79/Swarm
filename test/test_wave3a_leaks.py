#!/usr/bin/env python3
"""Wave 3a 资源/生命周期泄漏修复测试（TD2606-B15/B14）。"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_b15_reset_sandbox_pool_clears_singleton():
    """reset_sandbox_pool 停 reaper + drain + 清单例（杜绝 pool 指向死 manager 的 churn）。"""
    import swarm.worker.sandbox_pool as sp

    class _FakePool:
        def __init__(self):
            self.stopped = False
            self.drained = False

        def stop_reaper(self):
            self.stopped = True

        def drain(self):
            self.drained = True

    fake = _FakePool()
    sp._pool_singleton = fake
    try:
        sp.reset_sandbox_pool()
        assert sp._pool_singleton is None, "单例应被清空"
        assert fake.stopped and fake.drained, "应停 reaper 且 drain"
    finally:
        sp._pool_singleton = None
    print("  ✅ B15：reset_sandbox_pool 停 reaper+drain+清单例")


def test_b15_reset_manager_resets_pool():
    """reset_sandbox_manager 连带重置热池单例。"""
    import swarm.worker.sandbox as sb
    import swarm.worker.sandbox_pool as sp

    class _FakePool:
        def stop_reaper(self):
            pass

        def drain(self):
            pass

    sp._pool_singleton = _FakePool()
    sb.reset_sandbox_manager()
    assert sp._pool_singleton is None, "reset_sandbox_manager 应连带清热池单例"
    print("  ✅ B15：reset_sandbox_manager 连带重置热池")


def test_b14_cancel_standalone_worker():
    """卡死的后台单跑可被外部取消（句柄不再被丢弃）。"""
    import swarm.worker.runner as r

    assert r.cancel_standalone_worker("unknown-run") is False

    async def _main():
        async def _sleep_forever():
            await asyncio.sleep(100)

        t = asyncio.create_task(_sleep_forever())
        r._worker_tasks["run-x"] = t
        assert r.cancel_standalone_worker("run-x") is True
        await asyncio.sleep(0)
        assert t.cancelled()
        r._worker_tasks.pop("run-x", None)

    asyncio.run(_main())
    print("  ✅ B14：后台单跑可外部取消")


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
