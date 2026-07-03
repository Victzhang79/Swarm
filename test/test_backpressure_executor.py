#!/usr/bin/env python3
"""P1-C：连接池-线程池背压对齐单测。

修前：~142 处 run_in_executor(None, <同步 DB 调用>) + asyncio.to_thread 共享 asyncio 默认线程池
(~min(32,cpu+4) 线程)；连接池 max_size 默认硬编码 10 → 连接数 < 可并发发起 DB 调用的线程数 →
多余线程在 pool.connection() 排队撞 30s → PoolTimeout（高并发批量失败）。
修后（安全方向）：把连接数默认抬到 ≥ 默认线程池上限 min(32,cpu+4)，杜绝超订；【不缩线程池】——
缩线程池会饿死沙箱 HTTP/子进程等非 DB 阻塞工作（把有界的 30s PoolTimeout 换成无界全局卡死）。

纯逻辑，不依赖真 DB。
"""

from __future__ import annotations

import os

import swarm.infra.db as db


def _expected_ceiling() -> int:
    return min(32, (os.cpu_count() or 1) + 4)


def test_default_pool_max_matches_executor_ceiling(monkeypatch):
    monkeypatch.delenv("SWARM_DB_POOL_MAX", raising=False)
    _, pmax = db._pool_size()
    assert pmax == _expected_ceiling(), "默认 pool_max 必须对齐 asyncio 默认线程池上限 min(32,cpu+4)"


def test_default_pool_max_covers_default_thread_pool(monkeypatch):
    """核心不变量：连接数 ≥ 可并发发起 DB 调用的线程数 → 无 PoolTimeout 超订。"""
    monkeypatch.delenv("SWARM_DB_POOL_MAX", raising=False)
    _, pmax = db._pool_size()
    assert pmax >= _expected_ceiling()


def test_env_override_respected(monkeypatch):
    monkeypatch.setenv("SWARM_DB_POOL_MAX", "50")
    _, pmax = db._pool_size()
    assert pmax == 50


def test_env_invalid_falls_back_to_ceiling(monkeypatch):
    monkeypatch.setenv("SWARM_DB_POOL_MAX", "notanumber")
    _, pmax = db._pool_size()
    assert pmax == _expected_ceiling()


def test_pmin_clamped_leq_pmax(monkeypatch):
    monkeypatch.delenv("SWARM_DB_POOL_MAX", raising=False)
    monkeypatch.setenv("SWARM_DB_POOL_MIN", "999")  # 荒谬大 → 夹到 pmax
    pmin, pmax = db._pool_size()
    assert pmin <= pmax


def test_executor_is_not_bounded_no_starvation_regression():
    """回归守卫：不得再有 set_default_executor / 缩线程池的实现（会饿死非 DB 阻塞工作）。"""
    import inspect

    src = inspect.getsource(db)
    assert "set_default_executor" not in src, "不得缩/替换共享默认线程池（P1-C 对抗复核 F1：饿死沙箱 HTTP）"
    assert "configure_bounded_executor" not in src


def test_connect_timeout_reads_env(monkeypatch):
    """F2：SWARM_DB_CONNECT_TIMEOUT 可调，默认 10s。"""
    monkeypatch.setenv("SWARM_DB_CONNECT_TIMEOUT", "5")
    assert db._connect_timeout() == 5.0
    monkeypatch.delenv("SWARM_DB_CONNECT_TIMEOUT", raising=False)
    assert db._connect_timeout() == 10.0


def test_conn_kwargs_sets_connect_timeout(monkeypatch):
    """F2：>0 时注入 connect_timeout（绑住建连挂起）；autocommit 保持。"""
    monkeypatch.setattr(db, "_CONNECT_TIMEOUT_SEC", 10.0)
    kw = db._conn_kwargs()
    assert kw["autocommit"] is True
    assert kw.get("connect_timeout") == 10.0


def test_conn_kwargs_connect_timeout_disabled(monkeypatch):
    """<=0 → 不注入（回退 libpq 默认，不引入误配硬超时）。"""
    monkeypatch.setattr(db, "_CONNECT_TIMEOUT_SEC", 0.0)
    kw = db._conn_kwargs()
    assert "connect_timeout" not in kw


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
