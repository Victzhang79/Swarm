#!/usr/bin/env python3
"""3rd-P1d 回归：交付 git 写临界区在 per-project flock 内原子完成。

同项目跨模块的并发任务(plan 后 ModuleLock 升级、default 已释放)可同时到 learn_success →
reset+apply+commit 交错 → git index.lock 互踩/交错 commit → 交付损坏。治本=整段收进
_ProjectGitFlock，串行化同项目真仓写。纯函数/真 git 仓，无并发不确定性。
"""

from __future__ import annotations

import logging
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


def test_commit_post_observation_failure_emits_warning(tmp_path, monkeypatch, caplog):
    import swarm.project.diff_apply as diff_apply

    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    real_run = subprocess.run

    def _run(args, *pos, **kwargs):
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="probe failed")
        return real_run(args, *pos, **kwargs)

    monkeypatch.setattr(diff_apply.subprocess, "run", _run)
    with caplog.at_level(logging.WARNING, logger="swarm.project.diff_apply"):
        out = diff_apply.commit_task_output(str(repo), ["a.txt"], task_id="warn")

    assert out["ok"] is True
    assert out["committed"] is True
    assert "observation_warning" in out
    assert "读取 HEAD 失败" in caplog.text


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


def test_deliver_locked_refuses_to_overwrite_user_edit(tmp_path):
    """目标文件存在非任务补丁内容时，交付必须 fail-closed 并保留用户编辑。"""
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-base\n+delivered\n"
    )
    (repo / "a.txt").write_text("user edit\n")

    res = _deliver_merged_diff_locked(str(repo), diff, base, ["a.txt"], "task-1")

    assert res["ap"].get("ok") is False
    assert res["ap"].get("stage") == "worktree_conflict"
    assert (repo / "a.txt").read_text() == "user edit\n"
    assert _git(repo, "rev-parse", "HEAD") == base


def test_deliver_locked_commits_exact_worker_pullback(tmp_path):
    """工作区若精确等于 merged_diff 期望树，可直接固化，不误判为用户冲突。"""
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-base\n+delivered\n"
    )
    (repo / "a.txt").write_text("delivered\n")

    res = _deliver_merged_diff_locked(str(repo), diff, base, ["a.txt"], "task-1")

    assert res["ap"].get("ok") is True
    assert res["ap"].get("stage") == "already_present"
    assert res["commit"].get("committed") is True


def test_deliver_monorepo_subdir_reconciles_without_deleting_file(tmp_path):
    """monorepo 子目录的 diff 路径按项目根解释，不得被 git 错解为仓根路径。"""
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = _mkrepo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("base\n")
    _git(repo, "add", "sub/a.txt")
    _git(repo, "commit", "-qm", "sub-base")
    base = _git(repo, "rev-parse", "HEAD")
    diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-base\n+changed\n"

    res = _deliver_merged_diff_locked(str(sub), diff, base, ["a.txt"], "mono")

    assert res["ap"]["ok"] is True, res
    assert (sub / "a.txt").read_text() == "changed\n"
    assert _git(repo, "show", "HEAD:sub/a.txt") == "changed"


def test_deliver_unborn_greenfield_reconciles_and_creates_first_commit(tmp_path):
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = tmp_path / "unborn"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-base\n+changed\n"

    res = _deliver_merged_diff_locked(str(repo), diff, None, ["a.txt"], "green")

    assert res["ap"]["ok"] is True, res
    assert (repo / "a.txt").read_text() == "changed\n"
    assert _git(repo, "show", "HEAD:a.txt") == "changed"


def test_deliver_locked_fails_closed_when_conflict_probe_breaks(tmp_path, monkeypatch):
    """git 状态探针失败不能与“真无冲突”共用空列表。"""
    import swarm.git_base as git_base
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    diff = (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-base\n+delivered\n"
    )
    monkeypatch.setattr(
        git_base,
        "files_changed_since_base",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git failed")),
    )
    res = _deliver_merged_diff_locked(str(repo), diff, base, ["a.txt"], "task-1")
    assert res["ap"]["ok"] is False
    assert res["ap"]["stage"] == "worktree_conflict_check_failed"
    assert (repo / "a.txt").read_text() == "base\n"


def test_deliver_locked_rolls_back_partial_apply_before_manifest_or_commit(
    tmp_path, monkeypatch,
):
    """merged diff 只落下一部分时，整次交付失败且不得固化半套产物。"""
    import swarm.project.diff_apply as diff_apply
    import swarm.worker.workspace_manifest as workspace_manifest
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-base\n+delivered\n"
        "diff --git a/missing.txt b/missing.txt\n"
        "--- a/missing.txt\n+++ b/missing.txt\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    manifest_calls: list[str] = []
    commit_calls: list[list[str]] = []

    monkeypatch.setattr(
        workspace_manifest,
        "reconcile_workspace_manifests",
        lambda project_path: manifest_calls.append(project_path) or {},
    )
    monkeypatch.setattr(
        diff_apply,
        "commit_task_output",
        lambda _project_path, files, **_kwargs: (
            commit_calls.append(list(files))
            or {"ok": True, "committed": True, "commit_hash": "must-not-happen"}
        ),
    )

    res = _deliver_merged_diff_locked(
        str(repo), diff, base, ["a.txt", "missing.txt"], "task-partial",
    )

    assert res["ap"]["ok"] is False, res
    assert res["ap"]["stage"] == "apply_partial_failure"
    assert res["ap"]["applied"] == ["a.txt"]
    assert res["ap"]["failed"]
    assert res["ap"]["rollback_failed"] == []
    assert manifest_calls == [], "半套 diff 不得进入聚合清单 reconcile"
    assert commit_calls == [], "半套 diff 不得进入 commit"
    assert (repo / "a.txt").read_text() == "base\n"
    assert not (repo / "missing.txt").exists()
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == base


def test_deliver_locked_reports_partial_apply_rollback_failure_without_commit(
    tmp_path, monkeypatch,
):
    """回滚自身失败时必须留未提交脏树并显式报错，仍不得 commit。"""
    import swarm.brain.integration_review as integration_review
    import swarm.project.diff_apply as diff_apply
    import swarm.worker.workspace_manifest as workspace_manifest
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-base\n+delivered\n"
        "diff --git a/missing.txt b/missing.txt\n"
        "--- a/missing.txt\n+++ b/missing.txt\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    reset_calls = 0
    restore_calls = 0

    def _reset(*_args, **_kwargs):
        nonlocal reset_calls
        reset_calls += 1
        return []

    def _restore_then_fail(*_args, **_kwargs):
        nonlocal restore_calls
        restore_calls += 1
        return ["a.txt"]

    monkeypatch.setattr(
        integration_review, "_reset_worktree_to_head", _reset,
    )
    monkeypatch.setattr(
        integration_review, "restore_worktree_to_diff_baseline", _restore_then_fail,
    )
    monkeypatch.setattr(
        workspace_manifest,
        "reconcile_workspace_manifests",
        lambda *_args: (_ for _ in ()).throw(AssertionError("不得 reconcile")),
    )
    monkeypatch.setattr(
        diff_apply,
        "commit_task_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不得 commit")),
    )

    res = _deliver_merged_diff_locked(
        str(repo), diff, base, ["a.txt", "missing.txt"], "task-rollback-fail",
    )

    assert reset_calls == 1
    assert restore_calls == 1
    assert res["ap"]["ok"] is False
    assert res["ap"]["stage"] == "apply_partial_rollback_failed"
    assert res["ap"]["rollback_failed"] == ["a.txt"]
    assert res["commit"] == {}
    assert (repo / "a.txt").read_text() == "delivered\n"
    assert _git(repo, "status", "--porcelain") == "M a.txt"
    assert _git(repo, "rev-parse", "HEAD") == base


def test_deliver_locked_rolls_back_when_resilient_apply_raises_after_write(
    tmp_path, monkeypatch,
):
    """按文件 apply 中途抛异常也必须恢复基线，不能遗留半套工作树。"""
    import swarm.project.diff_apply as diff_apply
    import swarm.worker.workspace_manifest as workspace_manifest
    from swarm.brain.nodes import _deliver_merged_diff_locked

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n+++ b/a.txt\n"
        "@@ -1 +1 @@\n-base\n+delivered\n"
    )

    def _write_then_raise(project_path, _merged_diff):
        (Path(project_path) / "a.txt").write_text("delivered\n")
        raise RuntimeError("apply worker crashed after first write")

    monkeypatch.setattr(diff_apply, "apply_git_diff_resilient", _write_then_raise)
    monkeypatch.setattr(
        workspace_manifest,
        "reconcile_workspace_manifests",
        lambda *_args: (_ for _ in ()).throw(AssertionError("不得 reconcile")),
    )
    monkeypatch.setattr(
        diff_apply,
        "commit_task_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不得 commit")),
    )

    res = _deliver_merged_diff_locked(
        str(repo), diff, base, ["a.txt"], "task-apply-exception",
    )

    assert res["ap"]["ok"] is False
    assert res["ap"]["stage"] == "apply_exception"
    assert "apply worker crashed" in res["ap"]["reason"]
    assert res["ap"]["rollback_failed"] == []
    assert res["commit"] == {}
    assert (repo / "a.txt").read_text() == "base\n"
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == base


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
