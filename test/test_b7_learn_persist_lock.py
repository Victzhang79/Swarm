"""B7（round22, P1）：learn 幂等检查在事务外(TOCTOU) + L5/L6 reinforce 读改写无锁 → 并发双写。

根因：_already_persisted 检查在事务外；并发同键 learn 都通过检查后各写一条 → 重复 L2 +
双计 occurrence/reuse_count（污染衰减权重）。

治本（单 brain 进程拓扑）：进程级 async 锁把"检查幂等→写入(含 reinforce)"临界区串行化，
原子闭合 TOCTOU。本测试：并发两次同键 learn，断言只落一次（锁生效）。
"""
from __future__ import annotations

import asyncio

from unittest.mock import patch

from swarm.brain import learn_store

_KEYS: set = set()
_COUNTS: dict = {}


class _FakeStore:
    async def connect(self):
        pass

    async def close(self):
        pass

    async def summary_has_idempotency_key(self, pid, key):
        await asyncio.sleep(0)  # 让出，制造并发交错窗口
        return key in _KEYS

    def transaction(self):
        class _T:
            async def __aenter__(s):
                return None

            async def __aexit__(s, *a):
                return False
        return _T()

    async def query_mistakes(self, *a, **k):
        return []

    async def write_mistake(self, *a, **k):
        await asyncio.sleep(0.01)
        _COUNTS["mistake"] = _COUNTS.get("mistake", 0) + 1
        return 1

    async def write_task_summary(self, pid, ts):
        await asyncio.sleep(0.01)
        _KEYS.add(ts.metadata.get("idempotency_key"))
        _COUNTS["summary"] = _COUNTS.get("summary", 0) + 1


def test_concurrent_same_key_learn_dedups_to_one_write():
    _KEYS.clear(); _COUNTS.clear()
    state = {"project_id": "p1", "task_id": "t1"}

    def _fake_mistake(*a, **k):
        return {"error_type": "E", "description": "D", "context": "",
                "fix_description": "f", "tags": [], "code_snippet": ""}

    def _fake_l2(*a, **k):
        return {"summary": "SAME-SUMMARY", "lessons_learned": "", "metadata": {}}

    async def _run():
        with patch.object(learn_store, "MemoryStore", _FakeStore), \
             patch.object(learn_store, "build_mistake_payload", _fake_mistake), \
             patch.object(learn_store, "build_l2_summary", _fake_l2):
            await asyncio.gather(
                learn_store.persist_learn_failure(state, {}),
                learn_store.persist_learn_failure(state, {}),
            )

    asyncio.run(_run())
    assert _COUNTS.get("summary", 0) == 1, f"并发同键 learn 应只落一次 L2，实际 {_COUNTS}"
    assert _COUNTS.get("mistake", 0) == 1, f"并发同键 learn 应只写一次 L5，实际 {_COUNTS}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
