"""Wave R — router/infra 韧性回归测试。

覆盖：
- A-P1-13：Redis 不可用不永久锁存，冷却到期可重连。
- A-P1-14：ModuleLock.renew() 原子续期 TTL。
- A-P1-15：多 cloud provider 无显式映射时启发式告警。
- A-P1-16：连接池构造带 timeout/max_lifetime/check 安全参数。
"""

from __future__ import annotations

import logging

import pytest


# ── A-P1-13：Redis 重探 ────────────────────────────────
def test_redis_reprobe_after_cooldown(monkeypatch):
    """首次连接失败不永久锁存；冷却到期后下次访问可成功重连。"""
    import swarm.infra.redis_client as rc

    # 重置模块级状态。
    monkeypatch.setattr(rc, "_redis_client", None)
    monkeypatch.setattr(rc, "_redis_unavailable_at", None)
    monkeypatch.setenv("SWARM_REDIS_ENABLED", "true")
    # 冷却设极短便于测试。
    monkeypatch.setenv("SWARM_REDIS_REPROBE_COOLDOWN_SEC", "0")

    calls = {"n": 0}

    class _FakeRedis:
        def ping(self):
            return True

    class _FakeRedisModule:
        @staticmethod
        def from_url(uri, decode_responses=True, **kwargs):  # D14 后带 socket 超时 kwargs
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("transient blip")
            return _FakeRedis()

    monkeypatch.setitem(__import__("sys").modules, "redis", _FakeRedisModule)

    # 第一次：失败 → None，但不应永久锁存。
    assert rc.get_redis() is None
    assert rc._redis_unavailable_at is not None  # 记录了不可用时间戳
    assert rc._redis_client is None

    # 冷却=0，第二次访问应重新尝试并成功连接（旧布尔锁存实现这里会永远 None）。
    client = rc.get_redis()
    assert client is not None
    assert calls["n"] == 2  # 确实第二次重试了


def test_redis_unavailable_latched_within_cooldown(monkeypatch):
    """冷却窗内不重试——避免每次访问都打挂的 Redis。"""
    import swarm.infra.redis_client as rc

    monkeypatch.setattr(rc, "_redis_client", None)
    monkeypatch.setattr(rc, "_redis_unavailable_at", None)
    monkeypatch.setenv("SWARM_REDIS_ENABLED", "true")
    monkeypatch.setenv("SWARM_REDIS_REPROBE_COOLDOWN_SEC", "9999")

    calls = {"n": 0}

    class _FakeRedisModule:
        @staticmethod
        def from_url(uri, decode_responses=True, **kwargs):  # D14 后带 socket 超时 kwargs
            calls["n"] += 1
            raise ConnectionError("down")

    monkeypatch.setitem(__import__("sys").modules, "redis", _FakeRedisModule)

    assert rc.get_redis() is None
    assert rc.get_redis() is None
    # 冷却窗很长 → 第二次不应再尝试连接。
    assert calls["n"] == 1


# ── A-P1-14：ModuleLock.renew ─────────────────────────
def test_module_lock_renew_memory_fallback(monkeypatch):
    """Redis 关闭时 renew() no-op 返回 True（内存锁不过期）。"""
    import swarm.infra.redis_client as rc

    monkeypatch.setattr(rc, "_redis_client", None)
    monkeypatch.setattr(rc, "_redis_unavailable_at", None)
    monkeypatch.setenv("SWARM_REDIS_ENABLED", "false")

    lock = rc.ModuleLock("p1", "m1")
    assert lock.acquire() is True
    assert lock.renew() is True


def test_module_lock_renew_unheld_returns_false():
    import swarm.infra.redis_client as rc

    lock = rc.ModuleLock("p1", "m1")
    # 未 acquire。
    assert lock.renew() is False


def test_module_lock_renew_uses_atomic_expire(monkeypatch):
    """renew() 走 Lua 原子 expire（仅当 token 匹配）。"""
    import swarm.infra.redis_client as rc

    evals = []

    class _FakeRedis:
        def eval(self, script, numkeys, *args):
            evals.append((script, args))
            assert "expire" in script
            return 1

    monkeypatch.setattr(rc, "get_redis", lambda: _FakeRedis())
    lock = rc.ModuleLock("p1", "m1", ttl_sec=123)
    lock._held = True
    lock._redis_held = True  # H-2 后：只有 Redis-held 锁 renew 才走 Lua expire
    assert lock.renew() is True
    assert len(evals) == 1
    # token 与 ttl 作为 ARGV 传入。
    script, args = evals[0]
    assert lock.token in args
    assert 123 in args


# ── A-P1-15：多云 provider 告警 ───────────────────────
class _CaptureHandler(logging.Handler):
    """自带 handler 直接挂到目标 logger——不依赖 caplog/propagation
    （全量跑时别的 conftest 可能改了根日志配置，caplog 捕不到）。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):  # noqa: D102
        self.records.append(record)


def _capture_provider_for_model(cfg, model_name):
    slog = logging.getLogger("swarm.config.settings")
    h = _CaptureHandler()
    prev_level = slog.level
    slog.addHandler(h)
    slog.setLevel(logging.WARNING)
    try:
        pc = cfg.provider_for_model(model_name)
    finally:
        slog.removeHandler(h)
        slog.setLevel(prev_level)
    msgs = [r.getMessage() for r in h.records]
    return pc, msgs


def test_provider_for_model_warns_multi_cloud():
    from swarm.config.settings import ModelConfig, ProviderConfig

    cfg = ModelConfig(
        providers=[
            ProviderConfig(id="cloudA", label="A", kind="cloud", base_url="https://a/v1"),
            ProviderConfig(id="cloudB", label="B", kind="cloud", base_url="https://b/v1"),
        ],
        model_providers={},  # 无显式映射
    )
    pc, msgs = _capture_provider_for_model(cfg, "vendor/some-model")
    # 行为不变：仍取第一个 cloud。
    assert pc is not None and pc.id == "cloudA"
    # 但应告警。
    assert any("provider_for_model" in m and "cloudA" in m for m in msgs)


def test_provider_for_model_no_warn_single_cloud():
    from swarm.config.settings import ModelConfig, ProviderConfig

    cfg = ModelConfig(
        providers=[
            ProviderConfig(id="cloudA", label="A", kind="cloud", base_url="https://a/v1"),
            ProviderConfig(id="local", label="L", kind="local", base_url="http://localhost"),
        ],
        model_providers={},
    )
    pc, msgs = _capture_provider_for_model(cfg, "vendor/some-model")
    assert pc is not None and pc.id == "cloudA"
    assert not any("provider_for_model" in m for m in msgs)


def test_provider_for_model_explicit_mapping_no_warn():
    """有显式映射时不告警（映射优先，无歧义）。"""
    from swarm.config.settings import ModelConfig, ProviderConfig

    cfg = ModelConfig(
        providers=[
            ProviderConfig(id="cloudA", label="A", kind="cloud", base_url="https://a/v1"),
            ProviderConfig(id="cloudB", label="B", kind="cloud", base_url="https://b/v1"),
        ],
        model_providers={"vendor/some-model": "cloudB"},
    )
    pc, msgs = _capture_provider_for_model(cfg, "vendor/some-model")
    assert pc is not None and pc.id == "cloudB"
    assert not any("provider_for_model" in m for m in msgs)


# ── A-P1-16：连接池安全参数 ───────────────────────────
def test_sync_pool_has_safety_params(monkeypatch):
    import swarm.infra.db as db

    captured = {}

    class _FakePool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, *a, **kw):
            captured.update(kw)

        def close(self):
            pass

    monkeypatch.setattr(db, "_sync_pools", {})
    monkeypatch.setattr(db, "ConnectionPool", _FakePool)
    db.sync_pool("postgresql://x/y")

    assert captured.get("timeout") == db._POOL_TIMEOUT_SEC
    assert captured.get("max_lifetime") == db._POOL_MAX_LIFETIME_SEC
    assert "check" in captured and captured["check"] is not None


@pytest.mark.asyncio
async def test_async_pool_has_safety_params(monkeypatch):
    import swarm.infra.db as db

    captured = {}

    class _FakeAsyncPool:
        check_connection = staticmethod(lambda conn: None)

        def __init__(self, *a, **kw):
            captured.update(kw)

        async def open(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(db, "_async_pools", {})
    monkeypatch.setattr(db, "AsyncConnectionPool", _FakeAsyncPool)
    await db.async_pool("postgresql://x/y")

    assert captured.get("timeout") == db._POOL_TIMEOUT_SEC
    assert captured.get("max_lifetime") == db._POOL_MAX_LIFETIME_SEC
    assert "check" in captured and captured["check"] is not None
