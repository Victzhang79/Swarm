#!/usr/bin/env python3
"""3rd#2 治本回归：任务级 base-commit 钉扎。

覆盖：leaf 解析/捕获/可达探测；merge base_reader 读钉扎 SHA（非漂移 HEAD）；
L2/learn 的 _reset_worktree_to_head 按 base_ref 复位；worker executor 基线用钉扎 ref；
run_task 捕获+落库、resume 读回不重捕获（wiring 源码守卫）。
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
    (repo / "a.txt").write_text("base-v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


# ── leaf: git_base ─────────────────────────────────────────

def test_resolve_base_ref_none_falls_back_head():
    from swarm.git_base import resolve_base_ref

    assert resolve_base_ref(None) == "HEAD"
    assert resolve_base_ref("") == "HEAD"
    assert resolve_base_ref("   ") == "HEAD"
    assert resolve_base_ref("abc123def456") == "abc123def456"


def test_capture_base_commit_git_repo(tmp_path):
    from swarm.git_base import capture_base_commit

    repo = _mkrepo(tmp_path)
    sha = capture_base_commit(str(repo))
    assert sha and len(sha) == 40
    assert sha == _git(repo, "rev-parse", "HEAD")


def test_capture_base_commit_non_git_returns_none(tmp_path):
    from swarm.git_base import capture_base_commit

    plain = tmp_path / "plain"
    plain.mkdir()
    assert capture_base_commit(str(plain)) is None
    assert capture_base_commit(None) is None


def test_base_ref_exists_detects_reachable_and_gc(tmp_path):
    from swarm.git_base import base_ref_exists, capture_base_commit

    repo = _mkrepo(tmp_path)
    sha = capture_base_commit(str(repo))
    assert base_ref_exists(str(repo), sha) is True
    assert base_ref_exists(str(repo), "0" * 40) is False   # 不可达
    assert base_ref_exists(str(repo), None) is False


# ── A7: merge base_reader 读钉扎 SHA，而非漂移后的 HEAD ────────

def test_make_base_reader_uses_pinned_base_not_moved_head(tmp_path, monkeypatch):
    """任务钉住 base_commit 后，即便 HEAD 前移一个 commit，base_reader 仍读【钉扎版】。"""
    from swarm.brain.nodes import _make_base_reader
    import swarm.brain.nodes as nodes

    repo = _mkrepo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")   # a.txt = base-v1
    # 模拟运行期用户/兄弟任务推进 HEAD：a.txt 改成 moved-v2 并提交
    (repo / "a.txt").write_text("moved-v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "moved")

    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: str(repo))
    state = {"project_id": "p", "base_commit": base_sha}
    reader = _make_base_reader(state)
    # 钉扎生效 → 读到 base 版（base-v1），不是漂移后的 HEAD（moved-v2）
    assert reader("a.txt") == "base-v1\n"


def test_make_base_reader_none_base_falls_back_head(tmp_path, monkeypatch):
    from swarm.brain.nodes import _make_base_reader
    import swarm.brain.nodes as nodes

    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("moved-v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "moved")

    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: str(repo))
    state = {"project_id": "p"}  # 无 base_commit → HEAD 行为（零回归）
    reader = _make_base_reader(state)
    assert reader("a.txt") == "moved-v2\n"


# ── A9: _reset_worktree_to_head 按钉扎 base_ref 复位 ──────────

def test_reset_worktree_to_base_ref(tmp_path):
    """给定 base_ref，reset 把涉及文件恢复到【钉扎版】而非当前 HEAD。"""
    from swarm.brain.integration_review import _reset_worktree_to_head

    repo = _mkrepo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")   # a.txt=base-v1
    (repo / "a.txt").write_text("moved-v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "moved")
    # 工作区再脏改
    (repo / "a.txt").write_text("dirty-v3\n")

    diff = "--- a/a.txt\n+++ b/a.txt\n"  # 涉及 a.txt
    _reset_worktree_to_head(str(repo), diff, base_ref=base_sha)
    assert (repo / "a.txt").read_text() == "base-v1\n", "应复位到钉扎 base 版"


def test_reset_worktree_default_head_unchanged_behavior(tmp_path):
    """不传 base_ref → 复位到 HEAD（现行为不回归）。"""
    from swarm.brain.integration_review import _reset_worktree_to_head

    repo = _mkrepo(tmp_path)
    (repo / "a.txt").write_text("dirty\n")
    diff = "--- a/a.txt\n+++ b/a.txt\n"
    _reset_worktree_to_head(str(repo), diff)  # base_ref 默认 None → HEAD
    assert (repo / "a.txt").read_text() == "base-v1\n"


# ── wiring 守卫（源码级，防回归）──────────────────────────────

def test_run_task_captures_and_persists_base_commit():
    import inspect
    from swarm.brain import runner

    src = inspect.getsource(runner.run_task)
    assert "capture_base_commit" in src, "run_task 未捕获 base_commit（3rd#2 回归）"
    assert '"base_commit"' in src or "base_commit=" in src, "run_task 未 seed/落库 base_commit"


def test_resume_never_recaptures_base_commit():
    """resume_task/resume_planning 绝不重捕获 base_commit——base 随 PG checkpoint 恢复的
    图状态回来（run_task 已 seed 进 initial_state → 落 checkpoint），沿用任务出生基线。
    重捕获会引入 resume 时刻的新 HEAD = 正是要消除的漂移。"""
    import inspect
    from swarm.brain import runner

    for fn in (runner.resume_task, runner.resume_planning):
        src = inspect.getsource(fn)
        assert "capture_base_commit" not in src, \
            f"{fn.__name__} 不应重捕获 base_commit（必须沿用出生基线，随 checkpoint 恢复）"


def test_worker_executor_threads_base_ref():
    """WorkerExecutor 接受 base_ref 并在 git 基线读取处用 resolve_base_ref。"""
    import inspect
    from swarm.worker import executor

    init_src = inspect.getsource(executor.WorkerExecutor.__init__)
    assert "base_ref" in init_src, "WorkerExecutor.__init__ 未接 base_ref"
    baseline_src = inspect.getsource(executor.WorkerExecutor._git_baseline_text)
    assert "resolve_base_ref" in baseline_src or "self.base_ref" in baseline_src, \
        "_git_baseline_text 未用钉扎 base_ref（A2 回归）"


def test_dispatcher_threads_base_ref():
    import inspect
    from swarm.infra import worker_dispatcher

    src = inspect.getsource(worker_dispatcher.InProcessDispatcher.dispatch)
    assert "base_ref" in src, "InProcessDispatcher.dispatch 未透传 base_ref"


# ── 3rd-P1b：交付基线偏移检测（与 base-pin 耦合，不静默覆盖用户 commit）─────

def test_worktree_diverged_from_base(tmp_path):
    from swarm.git_base import worktree_diverged_from_base

    repo = _mkrepo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    # 未偏移
    assert worktree_diverged_from_base(str(repo), base_sha) == (False, base_sha)
    # 用户中途 commit → HEAD 前移 → 偏移
    (repo / "a.txt").write_text("user-edit\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "user")
    diverged, head = worktree_diverged_from_base(str(repo), base_sha)
    assert diverged is True and head == _git(repo, "rev-parse", "HEAD")
    # 无 base → 不谈偏移
    assert worktree_diverged_from_base(str(repo), None) == (False, None)


def test_files_changed_since_base_finds_clobber_victims(tmp_path):
    """reset 到 base 会覆盖其【已提交】改动的受害文件被准确识别。"""
    from swarm.git_base import files_changed_since_base

    repo = _mkrepo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("user-committed-change\n")
    (repo / "b.txt").write_text("unrelated\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "user")
    # 交付涉及 a.txt 与 c.txt；只有 a.txt 在 base..HEAD 被改过 → 唯一受害者
    victims = files_changed_since_base(str(repo), base_sha, ["a.txt", "c.txt"])
    assert victims == ["a.txt"]
    # 无偏移文件 → 空
    assert files_changed_since_base(str(repo), base_sha, ["c.txt"]) == []
    assert files_changed_since_base(str(repo), None, ["a.txt"]) == []


def test_learn_success_has_divergence_guard():
    """learn_success 交付前含基线偏移 loud 告警 + audit（P1b 不静默覆盖）。"""
    import inspect
    from swarm.brain import nodes

    src = inspect.getsource(nodes.learn_success)
    assert "worktree_diverged_from_base" in src, "learn_success 缺基线偏移检测（P1b 回归）"
    assert "delivery_baseline_diverged" in src, "learn_success 缺偏移 audit 事件（P1b 回归）"


# ── 对抗复核 H2：GC'd / 不可达 base → reset 退回 HEAD，不误删跟踪文件 ────

def test_reset_unreachable_base_falls_back_head_not_delete(tmp_path):
    """钉扎 base 不可达（模拟历史被重写）→ reset 退回 HEAD 复位跟踪文件，绝不当"新建"删除。"""
    from swarm.brain.integration_review import _reset_worktree_to_head

    repo = _mkrepo(tmp_path)  # a.txt 已跟踪 = base-v1
    (repo / "a.txt").write_text("dirty\n")
    bogus = "0" * 40  # 不可达 SHA
    diff = "--- a/a.txt\n+++ b/a.txt\n"
    _reset_worktree_to_head(str(repo), diff, base_ref=bogus)
    # 不可达 base → 退回 HEAD → a.txt 复位（仍存在，内容=HEAD 版），未被误删
    assert (repo / "a.txt").is_file(), "跟踪文件不得因不可达 base 被误删（H2 回归）"
    assert (repo / "a.txt").read_text() == "base-v1\n"


# ── 对抗复核 M1：git 项目捕获不到 base → loud warn ─────────────

def test_run_task_warns_when_capture_returns_none_for_git_project():
    """run_task 源码含：git 项目却 base=None 时的 loud fallback 告警（M1）。"""
    import inspect
    from swarm.brain import runner

    src = inspect.getsource(runner.run_task)
    assert "不受钉扎保护" in src, "run_task 缺 base 捕获失败的可观测告警（M1 回归）"
    assert 'os.path.join(project_path, ".git")' in src


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
