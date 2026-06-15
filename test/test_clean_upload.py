"""回归：批次2-A 干净上传（_sync_to_sandbox 上传 git HEAD 内容而非脏磁盘）。

跨重试叠加根因：bootstrap 上传脏 project_path → LLM 在脏版本上叠加。A 把 writable
tracked 文件改用 HEAD 版写入临时 staging 上传。这里 mock manager 捕获 upload_root，
断言 staging 里 writable 文件是 HEAD 干净版、untracked 文件是真实磁盘版。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

from swarm.types import FileScope


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo_test_a"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "mod.py").write_text("# HEAD clean\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_clean_upload_uses_head_version(tmp_path, monkeypatch):
    from swarm.worker.executor import WorkerExecutor

    repo = _make_repo(tmp_path)
    # 弄脏 writable 文件（模拟上一轮 pull-back 写回）
    (repo / "mod.py").write_text("# HEAD clean\n# DIRTY\n")
    # untracked 文件（新建产物）
    (repo / "new.py").write_text("new content\n")

    captured = {}

    class _Mgr:
        def sync_files_to_sandbox(self, sandbox, local_root, rel_files, remote_root):
            # 捕获上传 root 下每个文件的实际内容
            captured["root"] = str(local_root)
            captured["contents"] = {}
            for rel in rel_files:
                p = Path(local_root) / rel
                captured["contents"][rel] = p.read_text() if p.is_file() else None
            return {"uploaded": len(rel_files), "errors": [], "files": rel_files}

    stub = SimpleNamespace()
    stub.project_path = str(repo)
    stub.effective_scope = FileScope(writable=["mod.py"], readable=[], create_files=["new.py"])
    stub._sandbox = object()
    stub._sandbox_manager = _Mgr()
    stub._log = lambda m: None
    stub._writable_files = WorkerExecutor._writable_files.__get__(stub)
    stub._scope_files = lambda: ["mod.py", "new.py"]
    stub._norm_rel = WorkerExecutor._norm_rel
    stub._git_baseline_text = WorkerExecutor._git_baseline_text.__get__(stub)
    stub._snapshot_scope_local = WorkerExecutor._snapshot_scope_local.__get__(stub)
    stub._sync_to_sandbox = WorkerExecutor._sync_to_sandbox.__get__(stub)

    # get_config 需要 remote workdir
    import swarm.worker.executor as ex_mod
    monkeypatch.setattr(ex_mod, "get_config", lambda: SimpleNamespace(
        sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")
    ))

    asyncio.run(stub._sync_to_sandbox("bootstrap"))

    # writable tracked 文件 mod.py 应上传 HEAD 版（无 DIRTY），不是脏磁盘
    assert captured["contents"]["mod.py"] == "# HEAD clean\n", \
        f"writable 应上传 HEAD 干净版，实际={captured['contents']['mod.py']!r}"
    # untracked 新建文件 new.py 应上传真实磁盘版（HEAD 无此文件）
    assert captured["contents"]["new.py"] == "new content\n"
    # 真实 project_path 未被改动（A 只改上传内容，不动本地盘）
    assert (repo / "mod.py").read_text() == "# HEAD clean\n# DIRTY\n", "本地磁盘不应被 A 改动"


if __name__ == "__main__":
    import tempfile
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
