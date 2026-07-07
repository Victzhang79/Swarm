"""P0-8 D14/D15 — infra 超时治本行为测试（不依赖真 Redis/PG，全部用 fake/monkeypatch）。

D14：Redis 客户端 socket 超时 + brain 事件循环 renew 降频/卸线程池。
D15：知识层直连 psycopg 补 connect_timeout + KB loop 同步桥接有界等待 + get_retriever 锁不挂死。
"""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

# ──────────────────────────────────────────────
# D14 — Redis socket 超时
# ──────────────────────────────────────────────


def test_redis_from_url_passes_socket_timeouts(monkeypatch):
    """get_redis() 建连必须带 socket_connect_timeout/socket_timeout（>0），
    否则 Redis 网络黑洞（丢包挂起非 refused）时 r.eval/ping 无限阻塞卡死事件循环。"""
    from swarm.infra import redis_client as rc

    captured: dict = {}

    class _FakeClient:
        def ping(self):
            return True

    def _from_url(uri, **kwargs):
        captured.update(kwargs)
        return _FakeClient()

    fake_redis = types.ModuleType("redis")
    fake_redis.from_url = _from_url
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setenv("SWARM_REDIS_ENABLED", "true")
    monkeypatch.setattr(rc, "_redis_client", None)
    monkeypatch.setattr(rc, "_redis_unavailable_at", None)
    try:
        client = rc.get_redis()
        assert client is not None
        sct = captured.get("socket_connect_timeout")
        st = captured.get("socket_timeout")
        assert sct is not None and float(sct) > 0, "缺 socket_connect_timeout（默认 None=无限等）"
        assert st is not None and float(st) > 0, "缺 socket_timeout（默认 None=无限等）"
    finally:
        rc._redis_client = None
        rc._redis_unavailable_at = None


def test_redis_socket_timeouts_env_override(monkeypatch):
    """超时值集中可配：环境变量覆盖生效；非法/<=0 回退安全默认（fail-closed，绝不回到无限等）。"""
    from swarm.infra import redis_client as rc

    monkeypatch.setenv("SWARM_REDIS_SOCKET_CONNECT_TIMEOUT_SEC", "5")
    monkeypatch.setenv("SWARM_REDIS_SOCKET_TIMEOUT_SEC", "7")
    assert rc._redis_socket_connect_timeout() == 5.0
    assert rc._redis_socket_timeout() == 7.0
    monkeypatch.setenv("SWARM_REDIS_SOCKET_CONNECT_TIMEOUT_SEC", "0")
    monkeypatch.setenv("SWARM_REDIS_SOCKET_TIMEOUT_SEC", "not-a-number")
    assert rc._redis_socket_connect_timeout() > 0, "<=0 必须回退安全默认，不允许关闭超时"
    assert rc._redis_socket_timeout() > 0


# ──────────────────────────────────────────────
# D14 — renew 降频（RenewPacer）
# ──────────────────────────────────────────────


class _FakeLock:
    def __init__(self, ttl_sec: int = 100):
        self.ttl_sec = ttl_sec


def test_renew_pacer_throttles_by_ttl_fraction(monkeypatch):
    """降频不变量：距上次 renew 不足 TTL 的一小部分（TTL/10）→ 跳过；到期 → 放行并重置计时。
    首次见到锁（刚 acquire，TTL 全新）→ 跳过。"""
    monkeypatch.delenv("SWARM_LOCK_RENEW_INTERVAL_SEC", raising=False)
    from swarm.infra.redis_client import RenewPacer

    lock = _FakeLock(ttl_sec=100)  # 间隔 = 100/10 = 10s
    p = RenewPacer()
    assert p.due(lock, now=1000.0) is False, "首见（刚 acquire）应跳过"
    assert p.due(lock, now=1005.0) is False, "不足间隔应跳过"
    assert p.due(lock, now=1010.5) is True, "到期应放行"
    assert p.due(lock, now=1011.0) is False, "刚 renew 过应跳过"
    assert p.due(lock, now=1021.0) is True


def test_renew_pacer_resets_on_lock_upgrade(monkeypatch):
    """plan 后锁升级换对象 → 重置计时且不立即 renew（新锁刚 acquire 即满 TTL）。"""
    monkeypatch.delenv("SWARM_LOCK_RENEW_INTERVAL_SEC", raising=False)
    from swarm.infra.redis_client import RenewPacer

    p = RenewPacer()
    old = _FakeLock(ttl_sec=100)
    assert p.due(old, now=0.0) is False
    assert p.due(old, now=15.0) is True
    new = _FakeLock(ttl_sec=100)
    assert p.due(new, now=16.0) is False, "换锁对象必须重置计时"
    assert p.due(new, now=20.0) is False
    assert p.due(new, now=26.5) is True


def test_renew_interval_env_override(monkeypatch):
    """SWARM_LOCK_RENEW_INTERVAL_SEC 可覆盖；非法值回退 TTL/10。"""
    from swarm.infra.redis_client import renew_interval_sec

    monkeypatch.setenv("SWARM_LOCK_RENEW_INTERVAL_SEC", "2.5")
    assert renew_interval_sec(3600) == 2.5
    monkeypatch.setenv("SWARM_LOCK_RENEW_INTERVAL_SEC", "garbage")
    assert renew_interval_sec(3600) == 360.0
    monkeypatch.delenv("SWARM_LOCK_RENEW_INTERVAL_SEC", raising=False)
    assert renew_interval_sec(3600) == 360.0
    assert renew_interval_sec(5) >= 1.0, "间隔有下限，防 0 间隔空转"


# ──────────────────────────────────────────────
# D15 — 直连 psycopg 补 connect_timeout
# ──────────────────────────────────────────────


def test_pg_connect_timeout_kwargs_default():
    """单一取值点：默认给出正整数 connect_timeout（与池共用 SWARM_DB_CONNECT_TIMEOUT）。"""
    from swarm.infra.db import pg_connect_timeout_kwargs

    kw = pg_connect_timeout_kwargs()
    assert isinstance(kw.get("connect_timeout"), int)
    assert kw["connect_timeout"] >= 1


def test_default_dsn_contains_connect_timeout():
    """settings 默认 DSN 带 connect_timeout（兜住未显式传 kwargs 的直连/checkpointer）。"""
    from swarm.config.settings import DatabaseConfig

    default_uri = DatabaseConfig.model_fields["postgres_uri"].default
    assert "connect_timeout=" in default_uri


class _FakeCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return None

    async def fetchone(self):
        return (1,)

    async def fetchall(self):
        return []


class _FakeAsyncConn:
    closed = False

    def cursor(self, *a, **k):
        return _FakeCursor()

    async def close(self):
        return None


@pytest.mark.parametrize(
    "modname,clsname",
    [
        ("swarm.knowledge.structure_index", "StructureIndexer"),
        ("swarm.knowledge.behavior_store", "BehaviorStore"),
        ("swarm.knowledge.norms_store", "NormsStore"),
        ("swarm.memory.store", "MemoryStore"),
    ],
)
def test_async_store_connect_passes_connect_timeout(monkeypatch, modname, clsname):
    """知识/记忆层直连 AsyncConnection.connect 必须带 connect_timeout>0（PG 黑洞不无限挂）。"""
    import importlib

    import psycopg

    mod = importlib.import_module(modname)
    cls = getattr(mod, clsname)

    captured: list[dict] = []

    async def _fake_connect(conninfo="", **kwargs):
        captured.append(kwargs)
        return _FakeAsyncConn()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _fake_connect)

    async def _noop(self):
        return None

    monkeypatch.setattr(cls, "ensure_tables", _noop, raising=True)

    store = cls()
    asyncio.run(store.connect())
    assert captured, "connect 未走 psycopg.AsyncConnection.connect"
    ct = captured[0].get("connect_timeout")
    assert isinstance(ct, int) and ct >= 1, f"{clsname} 直连缺 connect_timeout"


def test_coordination_conn_passes_connect_timeout(monkeypatch):
    """infra/coordination 专属长连接同样必须带 connect_timeout。"""
    import psycopg

    from swarm.infra.coordination import PgCoordinationBackend

    captured: list[dict] = []

    async def _fake_connect(conninfo="", **kwargs):
        captured.append(kwargs)
        return _FakeAsyncConn()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _fake_connect)
    backend = PgCoordinationBackend(postgres_uri="postgresql://u:p@h:5432/db")
    asyncio.run(backend._ensure_conn())
    assert captured
    ct = captured[0].get("connect_timeout")
    assert isinstance(ct, int) and ct >= 1


# ──────────────────────────────────────────────
# D15 — _run_on_kb_loop 有界等待 + 超时后不留僵尸
# ──────────────────────────────────────────────


def test_run_on_kb_loop_times_out_and_cancels(monkeypatch):
    """KB loop 上任务挂起 → fut.result 有界等待超时抛明确 TimeoutError（fail-fast 非静默 None），
    且挂起任务被 cancel（不留不可观测僵尸）。"""
    from swarm.knowledge import service

    state = {"cancelled": False}

    async def _hang():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        service._run_on_kb_loop(_hang(), timeout=0.2)
    assert time.monotonic() - t0 < 5, "必须在超时值附近快失败，而非无限等"
    deadline = time.monotonic() + 3
    while not state["cancelled"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert state["cancelled"], "超时后 KB loop 上的任务必须被取消（不留僵尸）"


def test_run_on_kb_loop_default_timeout_bounded(monkeypatch):
    """默认超时集中可配且有界（>0），绝不回到无限等。"""
    from swarm.knowledge import service

    monkeypatch.delenv("SWARM_KB_SYNC_TIMEOUT_SEC", raising=False)
    assert service._kb_sync_timeout_sec() > 0
    monkeypatch.setenv("SWARM_KB_SYNC_TIMEOUT_SEC", "42")
    assert service._kb_sync_timeout_sec() == 42.0
    monkeypatch.setenv("SWARM_KB_SYNC_TIMEOUT_SEC", "-1")
    assert service._kb_sync_timeout_sec() > 0, "<=0 回退安全默认，不允许无限等"


def test_get_retriever_lock_released_after_connect_hang(monkeypatch):
    """connect_all 挂起 → 有界超时抛错、单例不发布（不留半初始化）、锁必须释放：
    第二次调用不得死锁且会重试 connect。"""
    from swarm.knowledge import service
    from swarm.knowledge.retriever import SwarmRetriever

    monkeypatch.setattr(service, "_retriever", None)
    monkeypatch.setattr(service, "_retriever_async_lock", None)
    monkeypatch.setenv("SWARM_KB_CONNECT_ALL_TIMEOUT_SEC", "0.2")

    calls = {"n": 0}

    async def _hang_connect(self):
        calls["n"] += 1
        await asyncio.sleep(30)

    monkeypatch.setattr(SwarmRetriever, "connect_all", _hang_connect)

    with pytest.raises(TimeoutError):
        service._run_on_kb_loop(service.get_retriever(), timeout=5)
    assert service._retriever is None, "connect 失败不得发布半初始化单例"

    # 锁已释放：第二次调用不死锁（外层 5s 兜底若死锁会抛 TimeoutError 但 calls 不会 +1）
    with pytest.raises(TimeoutError):
        service._run_on_kb_loop(service.get_retriever(), timeout=5)
    assert calls["n"] == 2, "第二次调用未进入 connect —— 锁未释放（死锁）或单例半初始化残留"
    assert service._retriever is None


def test_query_knowledge_base_tool_reports_timeout(monkeypatch):
    """worker 工具调用方：检索超时 → 明确的失败文本（可观测降级），而非异常炸穿/无限等。"""
    from swarm.tools import knowledge_tools as kt

    monkeypatch.setattr(kt, "get_worker_project_id", lambda: "proj-1")

    def _boom(*a, **k):
        raise TimeoutError("knowledge KB-loop 调用超时（300s）")

    monkeypatch.setattr(kt, "retrieve_knowledge_sync", _boom)
    out = kt.query_knowledge_base.func(query="how")
    assert isinstance(out, str)
    assert "超时" in out
