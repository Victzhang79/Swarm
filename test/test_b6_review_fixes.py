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

def test_retry_clears_base_commit():
    import inspect
    from swarm.brain import runner

    src = inspect.getsource(runner.retry_task)
    assert 'base_commit=""' in src, "retry 未清 base_commit（#5 回归，会沿用旧 birth base）"


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
    assert got is True, "探测失败必须保守保留任务（#6 回归）"


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


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
