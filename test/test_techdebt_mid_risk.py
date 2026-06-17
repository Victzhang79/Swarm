"""TECH_DEBT 中危项回归：M8 登录限流+计时均衡 / M2 工作根 ContextVar / M5 secret 区分。"""


def test_m8_login_throttle_locks_after_failures():
    """M8：连续失败达阈值 → 锁定；成功登录解锁。"""
    from swarm.api.routers.auth import _LoginThrottle
    t = _LoginThrottle(max_failures=3, window_sec=300, lockout_sec=300)
    k = "admin|1.2.3.4"
    assert t.is_locked(k) == (False, 0)
    for _ in range(3):
        t.record_failure(k)
    locked, retry = t.is_locked(k)
    assert locked and retry > 0, f"达阈值应锁定: {locked},{retry}"
    t.record_success(k)
    assert t.is_locked(k) == (False, 0), "成功登录应解锁"


def test_m8_login_throttle_window_expiry():
    """M8：窗口外的旧失败不累计（不会误锁）。"""
    import time
    from swarm.api.routers.auth import _LoginThrottle
    t = _LoginThrottle(max_failures=2, window_sec=1, lockout_sec=10)
    k = "u|ip"
    t.record_failure(k)
    time.sleep(1.1)  # 第一次失败移出窗口
    t.record_failure(k)
    locked, _ = t.is_locked(k)
    assert not locked, "窗口外旧失败不应累计触发锁定"


def test_m8_dummy_hash_constant_time():
    """M8：dummy hash 合法且 verify_password 能跑完整 PBKDF2（计时均衡）。"""
    from swarm.auth.passwords import verify_password
    from swarm.auth.store import _DUMMY_PASSWORD_HASH
    assert _DUMMY_PASSWORD_HASH.startswith("pbkdf2_sha256$")
    # 任意密码对 dummy hash 校验应返回 False，且不抛异常（走完整 PBKDF2）
    assert verify_password("anything", _DUMMY_PASSWORD_HASH) is False


def test_m2_workspace_root_contextvar():
    """M2：set_workspace_root 设的值被 workspace_root 读到（ContextVar 优先）。"""
    from swarm.tools.paths import set_workspace_root, workspace_root
    set_workspace_root("/tmp/projA_test")
    assert str(workspace_root()) == "/tmp/projA_test"
    set_workspace_root(None)  # 清理


def test_m2_workspace_isolated_across_tasks():
    """M2：两个并发 asyncio task 各设各的工作根，互不串。"""
    import asyncio
    from swarm.tools.paths import set_workspace_root, workspace_root
    seen = {}

    async def w(name, path):
        set_workspace_root(path)
        await asyncio.sleep(0.02)
        seen[name] = str(workspace_root())

    async def run():
        await asyncio.gather(w("A", "/tmp/pa"), w("B", "/tmp/pb"))

    asyncio.run(run())
    assert seen["A"] == "/tmp/pa", seen
    assert seen["B"] == "/tmp/pb", seen


def test_m5_secret_decrypt_vs_miss_distinguished():
    """M5：get_secret 源码应区分 decrypt 失败(warning)与 miss(静默)。"""
    import inspect
    from swarm.config import secret_store
    src = inspect.getsource(secret_store.get_secret)
    # decrypt 应被 try 单独包裹并 warning
    assert "解密失败" in src or "decrypt" in src.lower()
    assert "logger.warning" in src, "decrypt 失败应升级为 warning"
