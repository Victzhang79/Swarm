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
    lock._last_ok_monotonic = time.monotonic() - 1  # 才 1s，远在窗口内
    assert lock.renew() is True


# ── 收口 wiring 守卫 ─────────────────────────────

def test_scheduler_drains_under_sustained_load():
    """复核 Item 3：_loop 无条件(非仅队列空)跑节流排水，满负载下陈滞项也能恢复。"""
    import inspect
    import swarm.brain.scheduler as sched

    src = inspect.getsource(sched.start_task_scheduler)
    # 队列空分支 + 无条件分支各一次 _maybe_drain_stranded
    assert src.count("_maybe_drain_stranded()") >= 2, "满负载排水未接（Item 3 回归）"


def test_runner_partial_msg_includes_rebase_dropped():
    """复核 H-1：PARTIAL log/SSE 含 rebase_dropped，不再 0+0 无解释。"""
    import inspect
    from swarm.brain import runner

    src = inspect.getsource(runner._handle_post_run) if hasattr(runner, "_handle_post_run") else ""
    # 落在 _handle_post_run 或其调用链；宽松地在 runner 模块级搜
    msrc = inspect.getsource(runner)
    assert "merge_rebase_dropped" in msrc and "rebase 超限" in msrc, "PARTIAL 未暴露 rebase_dropped（H-1）"


def test_revision_and_plan_thread_base_ref():
    """复核 H-2/L-1：revision(resolve_plan_conflicts) 与 plan(normalize_plan_scopes) 调用点
    都传 project_path+base_ref，pom 多写者判定不在 revision/plan 期读实时 HEAD。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes)
    # revision 调用 resolve_plan_conflicts 带 base_ref
    assert 'resolve_plan_conflicts(updated_plan,' in src and 'base_ref=state.get("base_commit")' in src
    # plan 调用 normalize_plan_scopes 带 base_ref
    assert "normalize_plan_scopes(task_plan, project_path=" in src


def test_learn_success_emits_degraded_on_unreachable_base():
    """复核 M-3：不可达 base 不再静默 DONE，并入 degraded_reasons 终态可观测。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes.learn_success)
    assert '_degraded.append("delivery_base_unreachable")' in src
    assert '"degraded_reasons"' in src


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
