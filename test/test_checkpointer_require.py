#!/usr/bin/env python3
"""P0-D：PG checkpointer 初始化失败时的 fail-fast 策略单测。

修前：init 失败默认降级 MemorySaver（除非显式 SWARM_REQUIRE_PG_CHECKPOINTER∈{1,true,yes}）。
     → 生产单进程一重启即丢中断 checkpoint，任务永久卡 CONFIRMING/DELIVERING。
修后：显式设 env var 以其为准；未设时【生产环境默认 require→raise】，开发/测试保留降级。

monkeypatch AsyncPostgresSaver 让 init 必失败；monkeypatch get_config().is_production()。
"""

from __future__ import annotations

import pytest

import swarm.brain.graph as graph


class _BoomSaver:
    @staticmethod
    def from_conn_string(uri):
        raise RuntimeError("simulated PG down")


class _BoomPool:
    check_connection = staticmethod(lambda conn: None)  # 构造参数求值先取此属性

    def __init__(self, *a, **k):
        raise RuntimeError("simulated PG down")


def _force_init_failure(monkeypatch, *, env_var, is_prod):
    # 语义演进（阶段5 E6）：init 主路径改 AsyncConnectionPool（运行期自愈），
    # from_conn_string 只剩 psycopg_pool 缺席的回退——注入点跟随主路径，双双拍死
    # 防任一分支打到真 PG。
    import psycopg_pool
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _BoomPool)
    monkeypatch.setattr(graph, "AsyncPostgresSaver", _BoomSaver)
    graph._pg_checkpointer = None
    graph._pg_checkpointer_cm = None

    class _Cfg:
        db = type("D", (), {"postgres_uri": "postgresql://x/y"})()

        def is_production(self):
            return is_prod

    monkeypatch.setattr(graph, "get_config", lambda: _Cfg())
    if env_var is None:
        monkeypatch.delenv("SWARM_REQUIRE_PG_CHECKPOINTER", raising=False)
    else:
        monkeypatch.setenv("SWARM_REQUIRE_PG_CHECKPOINTER", env_var)


async def test_unset_production_defaults_to_failfast(monkeypatch):
    _force_init_failure(monkeypatch, env_var=None, is_prod=True)
    with pytest.raises(RuntimeError, match="simulated PG down"):
        await graph.init_postgres_checkpointer()


async def test_unset_dev_degrades_to_memorysaver(monkeypatch):
    _force_init_failure(monkeypatch, env_var=None, is_prod=False)
    ok = await graph.init_postgres_checkpointer()
    assert ok is False  # 降级，不 raise
    assert graph._pg_checkpointer is None


async def test_explicit_require_overrides_dev(monkeypatch):
    # 开发环境但显式要求 PG → 仍 fail-fast（显式优先于 env 默认）。
    _force_init_failure(monkeypatch, env_var="1", is_prod=False)
    with pytest.raises(RuntimeError, match="simulated PG down"):
        await graph.init_postgres_checkpointer()


async def test_explicit_disable_overrides_production(monkeypatch):
    # 生产但显式关掉要求（如运维知情降级）→ 降级不 raise（显式优先）。
    _force_init_failure(monkeypatch, env_var="0", is_prod=True)
    ok = await graph.init_postgres_checkpointer()
    assert ok is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
