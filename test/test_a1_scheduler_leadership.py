"""A1 批2 真实验证：PG 选主互斥 + 接管 + 降级。

验证目标（设计文档批2步骤5）：
1. 两个独立 backend 抢同一 key，只有 1 个成功（互斥）。
2. leader close（连接断→advisory lock 释放）后，另一个能抢到（接管）。
3. SchedulerLeadership 在 backend=None 时降级为本进程即 leader（单机不变）。

测试铁律：_test_ 前缀 key，try/finally 释放。
"""

from __future__ import annotations

import asyncio
import uuid

from swarm.infra.coordination import PgCoordinationBackend
from swarm.infra.scheduler_leadership import SchedulerLeadership

KEY = f"_test_lead_{uuid.uuid4().hex[:8]}"


async def _mutual_exclusion_and_takeover():
    be1 = PgCoordinationBackend()
    be2 = PgCoordinationBackend()
    try:
        # 1. 互斥：be1 抢到，be2 抢不到
        got1 = await be1.try_acquire_leadership(KEY)
        got2 = await be2.try_acquire_leadership(KEY)
        assert got1 is True, "be1 应抢到 leadership"
        assert got2 is False, "be2 不应同时抢到（互斥）"
        print("  ✅ 互斥：同一 key 只有 1 个 backend 持有")

        # 2. 接管：be1 close 释放锁后，be2 能抢到
        await be1.close()
        # advisory lock 随会话关闭释放；给一点时间
        await asyncio.sleep(0.2)
        got2_after = await be2.try_acquire_leadership(KEY)
        assert got2_after is True, "be1 释放后 be2 应能接管"
        print("  ✅ 接管：leader 释放后另一副本抢到 leadership")
    finally:
        await be1.close()
        await be2.close()


def test_pg_leadership_mutual_exclusion_and_takeover():
    asyncio.run(_mutual_exclusion_and_takeover())


def test_leadership_degrades_without_backend():
    """backend=None → 降级本进程即 leader（单机不变）。"""
    async def _main():
        lead = SchedulerLeadership(None, KEY)
        became = await lead.try_become_leader()
        assert became is True
        assert lead.is_leader is True
        await lead.release()  # 不应抛
    asyncio.run(_main())


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  💥 {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n=== A1 批2 选主: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
