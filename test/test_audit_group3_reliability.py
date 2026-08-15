"""走查报告第3组可靠性：H8 ModuleLock uuid token + H3 failed_subtask_ids 语义。"""


def test_h8_modulelock_token_unique():
    """H8：两个 ModuleLock 实例 token 不同（旧用时间戳会碰撞）。"""
    from swarm.infra.redis_client import ModuleLock
    a = ModuleLock("p1", "m1")
    b = ModuleLock("p1", "m1")
    assert a.token != b.token, "token 应全局唯一(uuid)"
    assert len(a.token) >= 16, "uuid hex 应足够长"


def test_h8_release_uses_lua_atomic(monkeypatch):
    """H8：release 删 Redis key 走 Lua 原子脚本（比对+删除一次往返），绝不 get-then-del 两步。

    行为断言（去 getsource 焊死）：用 fake redis 记录调用序列——release 必须调 eval（原子 Lua），
    绝不出现 get 后再 delete 的非原子两步。"""
    import swarm.infra.redis_client as rc
    from swarm.infra.redis_client import ModuleLock, _LOCK_RELEASE_LUA

    calls: list = []

    class _FakeRedis:
        def set(self, *a, **k):
            calls.append(("set", a))
            return True

        def eval(self, script, numkeys, *a):
            calls.append(("eval", script, a))
            return 1

        def get(self, *a, **k):
            calls.append(("get", a))
            return None

        def delete(self, *a, **k):
            calls.append(("delete", a))
            return 1

    monkeypatch.setattr(rc, "get_redis", lambda: _FakeRedis())
    rc._reset_project_gates()
    lk = ModuleLock("proj-h8", "modA")
    assert lk.acquire() is True
    calls.clear()
    lk.release()
    evals = [c for c in calls if c[0] == "eval"]
    assert any(c[1] is _LOCK_RELEASE_LUA for c in evals), "release 应用 _LOCK_RELEASE_LUA 原子比对删除"
    assert not any(c[0] == "delete" for c in calls), "绝不 get-then-del 非原子两步"


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
    """批25 GS-5w 换锁：原命题「H3：dispatch 无条件回填 failed_subtask_ids（空也回填，
    不再 if failed_ids）——否则 last-write-wins 残留上轮失败列表，gates 误拒真正成功的运行」。

    行为锁：真跑 dispatch 两条出口——①空批早退（failed 为空）②真派发成功（failed 为空）——
    返回都恒含 failed_subtask_ids 键。任一分支改回「仅非空才回填」即红（键缺席）。"""
    import asyncio
    from unittest.mock import patch

    from swarm.brain.nodes.dispatch import dispatch
    from swarm.types import (
        Confidence,
        FileScope,
        SubTask,
        SubTaskDifficulty,
        TaskPlan,
        WorkerOutput,
    )

    def _sub(sid, deps=None):
        return SubTask(id=sid, description="x", difficulty=SubTaskDifficulty.MEDIUM,
                       scope=FileScope(writable=["a.x"], readable=[]), depends_on=deps or [])

    # ① 空批早退：st-up L1 未过 → st-down 不就绪 → to_dispatch 空。failed 为空也必须回填。
    plan1 = TaskPlan(subtasks=[_sub("st-up"), _sub("st-down", deps=["st-up"])])
    out1 = asyncio.run(dispatch({
        "plan": plan1, "dispatch_remaining": ["st-down"],
        "subtask_results": {"st-up": WorkerOutput(
            subtask_id="st-up", diff="", summary="", confidence=Confidence.LOW, l1_passed=False)},
        "failed_subtask_ids": [],
    }))
    assert "failed_subtask_ids" in out1 and out1["failed_subtask_ids"] == [], (
        "空批早退分支必须恒回填 failed_subtask_ids（空也回填，H3 回归）")

    # ② 真派发成功路径：failed 为空 + worker L1 通过 → 正常出口同样恒含该键
    plan2 = TaskPlan(subtasks=[_sub("st-1")], parallel_groups=[["st-1"]])

    async def _fake_worker(subtask, knowledge_context, **kw):
        return WorkerOutput(subtask_id=subtask.id, diff="+x\n", summary="",
                            confidence=Confidence.HIGH, l1_passed=True)

    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=_fake_worker):
        out2 = asyncio.run(dispatch({
            "task_id": "", "project_id": "", "plan": plan2,
            "subtask_results": {}, "dispatch_remaining": ["st-1"],
            "failed_subtask_ids": [], "knowledge_context": {},
        }))
    assert out2["subtask_results"]["st-1"].l1_passed is True, "前提：本轮 L1 必须真通过"
    assert "failed_subtask_ids" in out2 and out2["failed_subtask_ids"] == [], (
        "正常出口也必须恒回填 failed_subtask_ids（H3 回归）")
