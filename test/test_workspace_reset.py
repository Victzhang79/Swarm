"""回归：批次2-B workspace 干净基线 reset（跨轮脏叠加根治）。

子任务起点把 scope 内 git 跟踪文件 reset 到 HEAD，untracked 产物保留。
验证 _reset_scope_to_head 在真实临时 git 仓库的行为（隔离 _test_ 仓库，不碰生产）。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm.types import FileScope


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo_test"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "test")
    (repo / "tracked.py").write_text("# clean HEAD version\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _executor_stub(repo: Path, writable, scope_files, create_files=None):
    """构造一个最小 stub，只挂 _reset_scope_to_head 需要的属性/方法。"""
    from swarm.worker.executor import WorkerExecutor

    stub = SimpleNamespace()
    stub.project_path = str(repo)
    stub.effective_scope = FileScope(
        writable=writable, readable=[], create_files=create_files or []
    )
    logs = []
    stub._log = lambda m: logs.append(m)
    stub._logs = logs
    stub._writable_files = WorkerExecutor._writable_files.__get__(stub)
    stub._scope_files = lambda: scope_files
    stub._norm_rel = WorkerExecutor._norm_rel  # staticmethod，直接用不绑定
    stub._reset_scope_to_head = WorkerExecutor._reset_scope_to_head.__get__(stub)
    return stub


def test_reset_restores_dirty_tracked_file(tmp_path):
    repo = _make_repo(tmp_path)
    # 把 tracked.py 弄脏（模拟上一轮 pull-back 写回的改动）
    (repo / "tracked.py").write_text("# clean HEAD version\n# DIRTY appended\n")
    stub = _executor_stub(repo, writable=["tracked.py"], scope_files=["tracked.py"])
    n = stub._reset_scope_to_head()
    assert n == 1
    assert (repo / "tracked.py").read_text() == "# clean HEAD version\n", "应恢复到 HEAD 干净版"


def test_reset_preserves_untracked_artifact(tmp_path):
    repo = _make_repo(tmp_path)
    # untracked 新建产物（如 worker 在沙箱 create 的文件 pull-back 回来）
    (repo / "NewUtil.java").write_text("public class NewUtil {}\n")
    stub = _executor_stub(
        repo, writable=["tracked.py"], scope_files=["tracked.py", "NewUtil.java"],
        create_files=["NewUtil.java"],
    )
    stub._reset_scope_to_head()
    assert (repo / "NewUtil.java").exists(), "untracked 新建产物绝不能被 reset 清掉"
    assert (repo / "NewUtil.java").read_text() == "public class NewUtil {}\n"


def test_reset_skips_non_git(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x")
    stub = _executor_stub(plain, writable=["a.py"], scope_files=["a.py"])
    assert stub._reset_scope_to_head() == 0, "非 git 仓库优雅跳过"


def test_reset_disabled_by_env(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "tracked.py").write_text("# dirty\n")
    stub = _executor_stub(repo, writable=["tracked.py"], scope_files=["tracked.py"])
    os.environ["SWARM_WORKER_RESET_SCOPE"] = "false"
    try:
        assert stub._reset_scope_to_head() == 0
        assert "# dirty" in (repo / "tracked.py").read_text(), "关闭时不应 reset"
    finally:
        os.environ.pop("SWARM_WORKER_RESET_SCOPE", None)


if __name__ == "__main__":
    import tempfile
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== workspace reset: {len(fns)}/{len(fns)} passed ===")
