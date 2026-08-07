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


def test_m5_secret_decrypt_vs_miss_distinguished(monkeypatch, caplog, secret_store_state,
                                                 fake_secret_conn):
    """M5：decrypt 失败必须告警、真 miss 必须静默 —— 两者返回值同为 None，只有日志能分辨。

    ★行为级★：原实现只断源码里出现过 `decrypt` 字样 + 函数内**任意位置**有一个
    `logger.warning`。把 M5 的整个告警块删掉（＝原病复发，与 miss 逐字同构）后，
    `decrypt` 由 `plaintext = decrypt(row[0])` 满足、`logger.warning` 由底部
    **DB 失败分支**那个满足 ⇒ 两条断言皆真 ⇒ 绿（29 号文 T-A2，与 T-A1 是同一机制的
    第二个假守卫）。故这里断的是【可观测差异】本身，不是源码字样。
    """
    import logging

    # 夹具经 conftest 的 fixture 取用，不再 `from test.<兄弟测试文件> import`
    # ——那条路依赖 pytest 把 rootdir 插进 sys.path 才不撞 stdlib `test` 遮蔽（hunter F11）。
    ss = secret_store_state          # 快照+还原三份模块级状态（见 conftest）
    monkeypatch.setattr(ss, "_get_conn",
                        lambda conn_str=None: fake_secret_conn(("cipher-blob",)))
    monkeypatch.setattr(ss, "decrypt",
                        lambda _c: (_ for _ in ()).throw(ValueError("bad key")))

    # ① DB 里【有】密文但解不开 → 必须 WARNING（key 轮换/密文损坏，运维必须看见）
    ss._cache.clear()
    with caplog.at_level(logging.DEBUG, logger=ss.logger.name):
        assert ss.get_secret("K_BROKEN") is None
    broken_warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert broken_warns, "decrypt 失败必须升级为 WARNING（静默回退 .env 旧值会让轮换问题极难排查）"
    assert any("K_BROKEN" in r.getMessage() for r in broken_warns), "告警必须点名 key"

    # ② 真 miss（无此 secret）→ 必须静默（大多数部署根本没写过 secret_store，回退 .env 是预期）
    caplog.clear()
    monkeypatch.setattr(ss, "_get_conn", lambda conn_str=None: fake_secret_conn(None))
    ss._cache.clear()
    with caplog.at_level(logging.DEBUG, logger=ss.logger.name):
        assert ss.get_secret("K_ABSENT") is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "真 miss 必须静默——否则每个未迁移的 key 每 30s 刷一条 WARNING（G1-1b 要治的正是噪声）"
