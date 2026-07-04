#!/usr/bin/env python3
"""3rd-P1d 回归：交付 git 写临界区在 per-project flock 内原子完成。

同项目跨模块的并发任务(plan 后 ModuleLock 升级、default 已释放)可同时到 learn_success →
reset+apply+commit 交错 → git index.lock 互踩/交错 commit → 交付损坏。治本=整段收进
_ProjectGitFlock，串行化同项目真仓写。纯函数/真 git 仓，无并发不确定性。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _mkrepo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_deliver_locked_applies_and_commits(tmp_path):
    """交付助手在 flock 内完成 reset→apply→commit，产出正确落盘 + commit。"""
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-base\n+delivered\n"
    )
    res = _deliver_merged_diff_locked(str(repo), diff, base, ["a.txt"], "task-1")
    assert res["ap"].get("ok"), res
    assert (repo / "a.txt").read_text() == "delivered\n"
    assert res["commit"].get("ok"), res
    # commit 已落地：HEAD 前移，a.txt=delivered
    assert _git(repo, "show", "HEAD:a.txt") == "delivered"


def test_deliver_locked_acquires_project_flock():
    """交付助手源码在 _ProjectGitFlock 内做 reset+apply+commit（P1d 串行化守卫）。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes._deliver_merged_diff_locked)
    assert "_ProjectGitFlock" in src, "交付 git 写未收进 per-project flock（P1d 回归）"
    assert "commit_task_output" in src and "apply_git_diff_resilient" in src
    # reset/apply/commit 必须在同一 with 块内（原子），不得散在锁外
    assert "with _ProjectGitFlock" in src


def test_learn_success_uses_serialized_delivery():
    """learn_success 交付走 _deliver_merged_diff_serialized（asyncio.Lock 序列化，不再 4 段散 to_thread）。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes.learn_success)
    assert "_deliver_merged_diff_serialized" in src, "learn_success 未改用序列化交付（P1d 回归）"
    # 复核 Finding 2：wm_error 必须 loud
    assert "wm_error" in src, "learn_success 未记录清单对账异常（Finding 2 回归）"


def test_serialized_delivery_uses_asyncio_lock_not_blocking_pool():
    """复核 Finding 1：交付经 per-project asyncio.Lock 在事件循环层序列化，不让 N 个交付各占
    一个 blocked 线程池槽。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes._deliver_merged_diff_serialized)
    assert "asyncio" in src.lower() and "Lock()" in src, "序列化未用 asyncio.Lock（Finding 1 回归）"
    assert "_project_delivery_locks" in src, "缺 per-project 锁字典"
    assert "to_thread" in src, "锁内仍需单次 to_thread 拉起同步交付"


async def test_serialized_delivery_serializes_same_project(tmp_path):
    """同一 project 的两次并发交付被 asyncio.Lock 串行（结果均正确落地，无交错损坏）。"""
    import asyncio
    from swarm.brain.nodes import _deliver_merged_diff_serialized, _project_delivery_locks

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _project_delivery_locks.clear()
    diff = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-base\n+delivered\n"
    )
    # 两次并发同项目交付
    r1, r2 = await asyncio.gather(
        _deliver_merged_diff_serialized(str(repo), diff, base, ["a.txt"], "t1"),
        _deliver_merged_diff_serialized(str(repo), diff, base, ["a.txt"], "t2"),
    )
    # 两者都跑完且未崩；同 project 复用同一把锁（字典仅一条目）
    assert r1["ap"].get("ok") and r2["ap"].get("ok")
    assert len(_project_delivery_locks) == 1
    assert (repo / "a.txt").read_text() == "delivered\n"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
