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
    """H2：L2 失败 replan 自增 replan_count、走熔断（不再绕过无限重规划）。

    行为断言（去 getsource 焊死）：L2 失败分支早于 LLM 调用返回，可直接驱动。
    ①达上限 → escalate 且 replan_count 已计数；②未达上限 → replan_count 自增。
    """
    import asyncio

    from swarm.brain.nodes import handle_failure
    from swarm.config.settings import get_config
    from swarm.types import FileScope, SubTask, TaskPlan

    _max = get_config().model.max_retries
    plan = TaskPlan(subtasks=[SubTask(id="st-1", description="x", scope=FileScope(create_files=["a/A.java"]))])

    def _state(replan_count: int) -> dict:
        return {
            "verification_failure": "l2",
            "replan_count": replan_count,
            "failed_subtask_ids": [],
            "subtask_results": {},
            "plan": plan,
        }

    # ① 已达上限：再 replan 越限 → escalate（熔断生效，未绕过）+ 计数已自增
    out_over = asyncio.run(handle_failure(_state(_max)))
    assert out_over["failure_strategy"] == "escalate"
    assert out_over["replan_count"] == _max + 1, "L2 replan 必须计入熔断计数"

    # ② 未达上限：replan_count 自增（counts toward limit，非绕过）
    out_under = asyncio.run(handle_failure(_state(0)))
    assert out_under.get("replan_count") == 1, "L2 失败 replan 应自增 replan_count"


def test_h3_dispatch_always_returns_failed_ids():
    """H3：dispatch 源码应无条件回填 failed_subtask_ids（不再 if failed_ids）。"""
    import inspect

    import swarm.brain.nodes.dispatch as dispatch_mod
    src = inspect.getsource(dispatch_mod)
    assert 'result["failed_subtask_ids"] = failed_ids' in src
    # 不应再有仅非空才回填的写法
    assert "if failed_ids:\n        result[\"failed_subtask_ids\"]" not in src
