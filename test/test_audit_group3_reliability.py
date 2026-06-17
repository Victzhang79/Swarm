"""走查报告第3组可靠性：H8 ModuleLock uuid token + H3 failed_subtask_ids 语义。"""


def test_h8_modulelock_token_unique():
    """H8：两个 ModuleLock 实例 token 不同（旧用时间戳会碰撞）。"""
    from swarm.infra.redis_client import ModuleLock
    a = ModuleLock("p1", "m1")
    b = ModuleLock("p1", "m1")
    assert a.token != b.token, "token 应全局唯一(uuid)"
    assert len(a.token) >= 16, "uuid hex 应足够长"


def test_h8_release_uses_lua_atomic():
    """H8：release 走 Lua 原子脚本（源码不应再有 get-then-del 两步）。"""
    import inspect

    from swarm.infra.redis_client import ModuleLock
    src = inspect.getsource(ModuleLock.release)
    assert "eval" in src, "release 应用 Lua eval 原子比对删除"
    # 不应再有先 get 再 delete 的非原子两步
    assert not ("r.get(self.key) == self.token" in src and "r.delete" in src), \
        "不应保留 get-then-del 非原子写法"


def test_h2_l2_replan_counts_toward_limit():
    """H2：L2 失败分支的 handle_failure 源码应自增 replan_count（不再绕过熔断）。"""
    import inspect

    from swarm.brain import nodes
    src = inspect.getsource(nodes.handle_failure) if hasattr(nodes, "handle_failure") else ""
    if not src:
        # handle_failure 可能在别处；退而验证 __init__ 模块源码含 L2 计数逻辑
        import swarm.brain.nodes as _n
        src = inspect.getsource(_n)
    assert 'verification_failure") == "l2"' in src
    # L2 分支应引用 replan_count（自增）
    seg = src[src.find('verification_failure") == "l2"'):]
    seg = seg[:600]
    assert "replan_count" in seg, "L2 失败分支应自增 replan_count 走熔断"


def test_h3_dispatch_always_returns_failed_ids():
    """H3：dispatch 源码应无条件回填 failed_subtask_ids（不再 if failed_ids）。"""
    import inspect

    import swarm.brain.nodes.dispatch as dispatch_mod
    src = inspect.getsource(dispatch_mod)
    assert 'result["failed_subtask_ids"] = failed_ids' in src
    # 不应再有仅非空才回填的写法
    assert "if failed_ids:\n        result[\"failed_subtask_ids\"]" not in src
