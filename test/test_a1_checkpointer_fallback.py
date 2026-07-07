"""A1 批1 降级与单例行为单测（不依赖真 PG）。

验证：
- get_compiled_brain_graph 在 PG checkpointer 未初始化时回退 MemorySaver（单机/CI 开箱即用）。
- init_postgres_checkpointer 连不上时返回 False 并降级（不抛、不卡）。
- close 幂等。
"""

from __future__ import annotations

import asyncio

import swarm.brain.graph as g


def test_fallback_to_memory_when_no_pg():
    """未初始化 PG checkpointer 时，get_compiled_brain_graph 用 MemorySaver 不崩。"""
    g.reset_compiled_brain_graph()
    # 确保 PG 单例为空
    g._pg_checkpointer = None
    compiled = g.get_compiled_brain_graph()
    assert compiled is not None


def test_init_pg_returns_false_on_bad_uri(monkeypatch):
    """PG 连不上 → init 返回 False 并降级（不抛异常）。

    次序隔离：本测钉的是【未显式要求强制 PG】时的降级路径。全量套件下先跑的测试
    可能经 load_dotenv 把 .env 的 SWARM_REQUIRE_PG_CHECKPOINTER=1 灌进 os.environ，
    令 init 走 fail-fast raise（by design 的另一条路径）——显式摘掉该变量隔离。"""
    monkeypatch.delenv("SWARM_REQUIRE_PG_CHECKPOINTER", raising=False)
    g._pg_checkpointer = None
    g._pg_checkpointer_cm = None
    ok = asyncio.run(g.init_postgres_checkpointer("postgresql://nohost:1/nodb"))
    assert ok is False
    assert g._pg_checkpointer is None  # 未设置 → get_compiled 会走 MemorySaver


def test_close_is_idempotent():
    """close 在未初始化时也不应抛。"""
    g._pg_checkpointer = None
    g._pg_checkpointer_cm = None
    asyncio.run(g.close_postgres_checkpointer())  # 不抛即通过


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
    print(f"\n=== A1 批1 降级/单例: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
