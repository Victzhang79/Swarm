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


def test_learn_success_uses_locked_delivery():
    """learn_success 交付走 _deliver_merged_diff_locked 单次 to_thread（不再 4 段散 to_thread）。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes.learn_success)
    assert "_deliver_merged_diff_locked" in src, "learn_success 未改用原子交付助手（P1d 回归）"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
