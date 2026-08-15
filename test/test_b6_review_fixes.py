#!/usr/bin/env python3
"""B6 用户对抗复核回炉：#3 未提交保护 / #4 不可达 base / #5 retry 重捕获 / #6 探针瞬时故障 / #7 rebase-dropped 入 PARTIAL。"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()


def _mkrepo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


# ── #3：未提交改动探测 ─────────────────────────────

def test_uncommitted_changed_files_detects_dirty(tmp_path):
    from swarm.git_base import uncommitted_changed_files

    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("dirty edit\n")   # 未 commit
    (repo / "b.txt").write_text("new\n")           # untracked
    dirty = uncommitted_changed_files(str(repo), ["a.txt", "b.txt", "c.txt"])
    assert "a.txt" in dirty and "b.txt" in dirty and "c.txt" not in dirty
    # 干净文件不报
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "x")
    assert uncommitted_changed_files(str(repo), ["a.txt"]) == []


def test_learn_success_detects_uncommitted_and_unreachable():
    """learn_success 交付守卫含未提交(#3)与不可达 base(#4)探测 + audit。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes.learn_success)
    assert "uncommitted_changed_files" in src and "delivery_uncommitted_overwrite" in src, "缺未提交保护（#3）"
    assert "base_ref_exists" in src and "delivery_base_unreachable" in src, "缺不可达 base 告警（#4）"


# ── #5：retry 清 base_commit → 重捕获 ─────────────

async def test_retry_clears_base_commit(monkeypatch):
    """行为测试(替原 getsource 守卫)：retry_task 实际以 base_commit='' 调 update_task，
    令 run_task 重捕获当前 HEAD 为新基线。断言【可观测副作用】而非源码字符串。"""
    import swarm.brain.runner as runner
    from swarm.project import store

    captured: dict = {}
    monkeypatch.setattr(runner, "can_retry_task", lambda tid: (True, ""))
    monkeypatch.setattr(store, "get_task", lambda tid: {"id": tid, "project_id": "p", "description": "d"})
    monkeypatch.setattr(store, "update_task", lambda tid, **kw: captured.update(kw))
    runner._task_running.clear()

    async def _noop_run(*a, **k):
        return None

    monkeypatch.setattr(runner, "run_task", _noop_run)
    ok = await runner.retry_task("t1")
    assert ok is True
    assert captured.get("base_commit") == "", "retry 必须清空 base_commit 触发重捕获（#5 行为回归）"
    assert captured.get("status") == "SUBMITTED"


# ── #6：探针瞬时故障 ≠ 无 checkpoint ─────────────

async def test_checkpoint_probe_transient_failure_keeps_task(monkeypatch):
    """aget_state 抛异常(PG 瞬时) → 探针返 True(保留任务)，不误判无 checkpoint 而 kill。"""
    import swarm.brain.runner as runner

    class _Graph:
        async def aget_state(self, config):
            raise ConnectionError("pg blip")

    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _Graph())
    monkeypatch.setattr(runner.store, "get_task", lambda tid: {"id": tid, "project_id": "p", "thread_id": tid})
    got = await runner._has_pending_checkpoint("t1")
    # 复核 M-1：探测失败返 None（三态），对账保守保留但计数（非 True 静默永卡，非 False 误杀）
    assert got is None, "探测失败必须返 None 保守保留 + 计数（#6/M-1）"


async def test_checkpoint_probe_clean_none_still_fails(monkeypatch):
    """aget_state 干净返 None(确无快照) → 探针返 False(真孤儿判死)，区分于瞬时故障。"""
    import swarm.brain.runner as runner

    class _Graph:
        async def aget_state(self, config):
            return None

    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _Graph())
    monkeypatch.setattr(runner.store, "get_task", lambda tid: {"id": tid, "project_id": "p", "thread_id": tid})
    assert await runner._has_pending_checkpoint("t1") is False


# ── #7：merge_rebase_dropped 入 partial_delivery_ids ─

def test_rebase_dropped_flows_into_partial():
    from swarm.brain.gates import partial_delivery_ids, is_partial_delivery

    state = {"abandoned_subtask_ids": [], "give_up_isolated_ids": [], "merge_rebase_dropped": ["st-30"]}
    assert partial_delivery_ids(state) == ["st-30"]
    assert is_partial_delivery(state) is True
    # 三者并集去重保序
    state2 = {"abandoned_subtask_ids": ["a"], "give_up_isolated_ids": ["g"], "merge_rebase_dropped": ["a", "r"]}
    assert partial_delivery_ids(state2) == ["a", "g", "r"]


# ── 收口：Item 1 renew 墙钟闸 ─────────────────────

def test_renew_wallclock_gate_aborts_when_ttl_elapsed(monkeypatch):
    """瞬时失败且距上次续期 > TTL*0.8 → 判失锁(即便计数未到阈值)，防锁过期后双写。"""
    import time
    import swarm.infra.redis_client as rc

    class _Boom:
        def eval(self, *a, **k):
            raise ConnectionError("blip")

    monkeypatch.setattr(rc, "get_redis", lambda: _Boom())
    lock = rc.ModuleLock("p", "m", ttl_sec=10)
    lock._held = True
    lock._redis_held = True  # H-2 后：只有 Redis-held 锁 renew 才走 Lua（纯本地锁 no-op）
    lock._last_ok_monotonic = time.monotonic() - 9  # 距上次续期 9s > 10*0.8=8
    assert lock.renew() is False, "距上次续期超 TTL*0.8 必须判失锁（Item 1）"


def test_renew_wallclock_gate_tolerates_within_window(monkeypatch):
    """瞬时失败但距上次续期在 TTL*0.8 内 → 仍容忍(不误杀长任务)。"""
    import time
    import swarm.infra.redis_client as rc

    class _Boom:
        def eval(self, *a, **k):
            raise ConnectionError("blip")

    monkeypatch.setattr(rc, "get_redis", lambda: _Boom())
    monkeypatch.setenv("SWARM_LOCK_RENEW_TRANSIENT_MAX", "3")
    lock = rc.ModuleLock("p", "m", ttl_sec=100)
    lock._held = True
    lock._redis_held = True  # H-2 后：只有 Redis-held 锁 renew 才走 Lua（纯本地锁 no-op）
    lock._last_ok_monotonic = time.monotonic() - 1  # 才 1s，远在窗口内
    assert lock.renew() is True


# ── 收口 wiring 守卫 ─────────────────────────────

async def test_scheduler_drains_under_sustained_load(monkeypatch):
    """批25 GS-5w 换锁：原命题「复核 Item 3：_loop 无条件(非仅队列空)跑节流排水，满负载下
    陈滞项也能恢复」。

    行为锁：真启动调度消费循环并把 _inflight 填满——并发满 → 「队列空+有空槽」分支内的排水
    永不可达，此时 spy 仍观测到 _maybe_drain_stranded 被调 = 无条件排水真实接上。
    删掉 _loop 尾部那次无条件排水调用即红（spy 永不触发 → 超时失败）。"""
    import asyncio

    import swarm.brain.scheduler as sched

    drained = asyncio.Event()

    async def _spy_drain():
        drained.set()

    monkeypatch.setattr(sched, "_maybe_drain_stranded", _spy_drain)
    assert not sched.is_consumer_running(), "前提：本测试需独占调度器（已有消费者在跑会失真）"
    # 前提自证：填满并发槽 → len(_inflight) < _max_concurrent() 恒 False → 持续满负载形态
    saved_inflight = set(sched._inflight)
    sched._inflight.clear()
    sched._inflight.update(f"gs5w-fake-{i}" for i in range(sched._max_concurrent()))
    try:
        await sched.start_task_scheduler()
        try:
            await asyncio.wait_for(drained.wait(), timeout=5.0)
        finally:
            await sched.stop_task_scheduler()
    finally:
        sched._inflight.clear()
        sched._inflight.update(saved_inflight)


async def test_runner_partial_msg_includes_rebase_dropped(monkeypatch):
    """批25 GS-5w 换锁：原命题「复核 H-1：PARTIAL log/SSE 含 rebase_dropped，不再 0+0 无解释」。

    行为锁：真跑 _handle_post_run——终态唯一 PARTIAL 成因是 merge_rebase_dropped 时，
    complete 事件 message 必须点名 rebase 超限丢弃及子任务 id，DB 终态为 PARTIAL。
    删 _msg 的 rebase 拼接块、或 partial_delivery_ids 摘掉 merge_rebase_dropped，任一即红。
    （批25 顺手清掉原测试里的死赋值：getsource 结果赋给 src 从未消费，连同 hasattr 兜底
    退 "" 的形态一并删除。）"""
    import swarm.brain.runner as runner

    emitted: list[dict] = []
    updates: list[dict] = []

    async def _fake_emit(topic, event):
        emitted.append(event)

    monkeypatch.setattr(runner, "_emit", _fake_emit)
    monkeypatch.setattr(runner, "_sync_task_from_state", lambda tid, st: None)
    monkeypatch.setattr(runner, "_emit_task_notification", lambda *a, **k: None)
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"id": tid, "project_id": "p1", "description": "d"})
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: updates.append(kw))
    monkeypatch.setattr(runner.store, "estimate_token_usage", lambda **kw: {})
    monkeypatch.setattr(runner.store, "compute_task_duration_seconds", lambda rec: 1.0)

    state = {
        "task_id": "t-gs5w-rebase-partial", "task_description": "d",
        # 前提自证：无 abandoned/give_up/remaining/交付失败——唯一 PARTIAL 成因是 rebase 丢弃
        "abandoned_subtask_ids": [], "give_up_isolated_ids": [],
        "dispatch_remaining": [], "merge_rebase_dropped": ["st-30"],
    }
    from swarm.brain.gates import terminal_status
    assert terminal_status(state) == "PARTIAL", "前提：rebase_dropped 必须单独足以判 PARTIAL"

    await runner._handle_post_run("t-gs5w-rebase-partial", state, None)

    completes = [e for e in emitted if e.get("step") == "complete"]
    assert completes and completes[0].get("status") == "partial", (
        "rebase-only 场景终态必须 PARTIAL（静默 DONE=丢工作，#7/H-1 回归）")
    msg = completes[0].get("message") or ""
    assert "rebase 超限丢弃" in msg and "st-30" in msg, (
        "rebase-only PARTIAL 必须在消息里解释丢弃来源，否则 0+0 无解释（H-1 回归）: " + msg)
    assert any(u.get("status") == "PARTIAL" for u in updates), "DB 终态必须落 PARTIAL"


def test_revision_and_plan_thread_base_ref(monkeypatch, tmp_path):
    """批25 GS-5w 换锁：原命题「复核 H-2/L-1：revision(resolve_plan_conflicts) 与
    plan(normalize_plan_scopes) 调用点都传 project_path+钉扎 base_ref——pom 多写者判定不在
    revision/plan 期读实时 HEAD」。

    行为锁：真跑两个节点，spy 两函数实收 kwarg——base_ref 必须逐字来自 state["base_commit"]、
    project_path 必须真传。任一调用点丢 base_ref 即红（spy 收到 None/缺键）。"""
    import asyncio

    import swarm.brain.contract_utils as cu
    import swarm.brain.nodes as nodes
    from swarm.types import Complexity, FileScope, SubTask, SubTaskDifficulty, TaskPlan

    seen: dict[str, dict] = {}

    def _spy_resolve(plan_obj, **kw):
        seen["resolve"] = dict(kw)
        return {}

    def _spy_normalize(plan_obj, **kw):
        seen["normalize"] = dict(kw)
        return False

    monkeypatch.setattr(cu, "resolve_plan_conflicts", _spy_resolve)
    monkeypatch.setattr(cu, "normalize_plan_scopes", _spy_normalize)
    # 两函数都是节点体内惰性 import（from ... import 于调用点），patch 模块属性即生效
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: str(tmp_path))

    # ① revision：LLM 失败走默认修订子任务兜底分支，仍必经 resolve_plan_conflicts
    def _no_llm():
        raise RuntimeError("no llm in test")

    monkeypatch.setattr(nodes, "_get_brain_llm", _no_llm)
    asyncio.run(nodes.revision({
        "task_id": "t1", "project_id": "p1", "revision_feedback": "改一下",
        "task_description": "d", "merged_diff": "",
        "plan": TaskPlan(subtasks=[SubTask(
            id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
            scope=FileScope(writable=["a.x"], readable=[]))]),
        "base_commit": "base-rev-123",
    }))
    assert "resolve" in seen, "前提：revision 必须真调到 resolve_plan_conflicts"
    assert seen["resolve"].get("base_ref") == "base-rev-123", (
        "revision 必须把钉扎 base 传给 resolve_plan_conflicts（H-2 回归）")
    assert seen["resolve"].get("project_path") == str(tmp_path), (
        "revision 必须真传 project_path（None 会短路 aggregate-vs-新建撞车判定）")

    # ② plan：假 LLM 产合法单子任务计划，走到 normalize_plan_scopes 调用点
    class _FakeLLM:
        async def ainvoke(self, messages):
            class _R:
                content = ('{"subtasks":[{"id":"st-1","description":"x",'
                           '"scope":{"writable":["a"],"readable":[]}}],'
                           '"parallel_groups":[["st-1"]]}')
            return _R()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    asyncio.run(nodes.plan({
        "task_id": "t2", "project_id": "p1",
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "base_commit": "base-plan-456",
    }))
    assert "normalize" in seen, "前提：plan 必须真调到 normalize_plan_scopes"
    assert seen["normalize"].get("base_ref") == "base-plan-456", (
        "plan 必须把钉扎 base 传给 normalize_plan_scopes（L-1 回归）")
    assert seen["normalize"].get("project_path") == str(tmp_path), (
        "plan 必须真传 project_path（None 会短路撞车判定）")


async def test_learn_success_emits_degraded_on_unreachable_base(monkeypatch):
    """批25 GS-5w 换锁：原命题「复核 M-3：不可达 base 不再静默 DONE，并入 degraded_reasons
    终态可观测」。

    行为锁：真跑 learn_success——base_ref_exists 返 False（GC/历史重写致钉扎 base 不可达）→
    返回补丁的 degraded_reasons 必含 delivery_base_unreachable。删 _degraded.append 行即红。"""
    import swarm.brain.learn_store as learn_store
    import swarm.git_base as git_base
    import swarm.knowledge.hooks as hooks
    from swarm.brain import nodes
    from swarm.types import Complexity

    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: "/tmp/fake-proj")
    # 前提隔离：HEAD 未偏移、无未提交脏改——唯一异常=钉扎 base 不可达（B6 #4）
    monkeypatch.setattr(git_base, "worktree_diverged_from_base", lambda p, b: (False, "head123"))
    monkeypatch.setattr(git_base, "uncommitted_changed_files", lambda p, files: [])
    monkeypatch.setattr(git_base, "base_ref_exists", lambda p, b: False)  # 关键前提
    monkeypatch.setattr(nodes, "audit", lambda *a, **k: None)  # 审计落库与降级信号无关，隔掉
    monkeypatch.setattr(hooks, "schedule_incremental_update", lambda *a, **k: None)

    async def _fake_deliver(proj_path, merged_diff, base_commit, out_files, task_id):
        return {"ap": {"ok": True, "applied": ["a.py"], "failed": []},
                "out_files": ["a.py"], "wm": {},
                "commit": {"ok": True, "committed": True, "commit_hash": "abc"}}

    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _fake_deliver)

    async def _fake_persist(state, parsed):
        return {"persisted": False}

    monkeypatch.setattr(learn_store, "persist_learn_success", _fake_persist)

    out = await nodes.learn_success({
        "task_id": "t1", "project_id": "p1", "task_description": "d",
        "merged_diff": "--- a/a.py\n+++ b/a.py\n@@ +x\n",
        "complexity": Complexity.SIMPLE,  # SIMPLE 路径不走 LLM，聚焦交付降级面
        "base_commit": "deadbeefdeadbeef",  # 钉扎 base 已不可达（非 "HEAD"）
    })
    assert out.get("learned") is True, "前提：learn_success 必须真走完"
    assert "delivery_base_unreachable" in (out.get("degraded_reasons") or []), (
        "不可达 base 必须并入 degraded_reasons 终态可观测（M-3 回归）——静默 DONE=交付基线"
        "已损却报假成功")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
