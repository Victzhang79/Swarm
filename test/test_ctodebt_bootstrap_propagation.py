"""根因修复(task 69d34b1b)：自带源码沙箱 bootstrap 应传播【上游子任务产物】。

机制：上游脚手架子任务建的模块 pom（untracked）+ 注册了模块的父 pom（tracked 改动）在依赖
子任务里列为 readable。自带源码模式默认不传 readable → 依赖子任务沙箱缺失 → mvn -pl reactor
not found。修复：① _reset_scope_to_head 不再 reset readable（不抹上游改动）；② _sync_to_sandbox
补传"本地内容≠git HEAD"的 readable 文件。
"""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import MagicMock

from swarm.types import FileScope, SubTask
from swarm.worker.executor import WorkerExecutor


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_bootstrap_propagates_upstream_products(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "base.java").write_text("class Base{}\n")
    (proj / "parent_pom.xml").write_text("<modules></modules>\n")
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")
    # 上游产物：新建未跟踪模块 pom + 改父 pom（本地≠HEAD）
    (proj / "mod_pom.xml").write_text("<project/>\n")
    (proj / "parent_pom.xml").write_text("<modules><module>mod</module></modules>\n")

    st = SubTask(id="st-2", description="d", scope=FileScope(
        writable=[], readable=["base.java", "parent_pom.xml", "mod_pom.xml"]))
    ex = WorkerExecutor(st, project_path=str(proj))
    ex._sandbox_has_source = True
    ex._sandbox = object()

    captured: dict = {}
    mgr = MagicMock()

    def _cap(sb, root, rel_files, workdir):
        captured["rel"] = list(rel_files)
        return {"uploaded": len(rel_files), "errors": []}

    mgr.sync_files_to_sandbox.side_effect = _cap
    ex._sandbox_manager = mgr

    asyncio.run(ex._sync_to_sandbox("bootstrap"))
    rel = captured.get("rel", [])
    assert "mod_pom.xml" in rel, f"上游新建模块 pom(untracked) 应补传: {rel}"
    assert "parent_pom.xml" in rel, f"上游改过的父 pom(tracked≠HEAD) 应补传: {rel}"
    assert "base.java" not in rel, f"未改 readable(==HEAD) 不应补传: {rel}"


def test_reset_scope_no_longer_targets_readable():
    """_reset_scope_to_head 只 reset writable∪create，不碰 readable（防抹上游产物）。"""
    import inspect
    src = inspect.getsource(WorkerExecutor._reset_scope_to_head)
    assert "for f in self._writable_files():" in src
    assert "for f in self._scope_files():" not in src, "reset 不应再遍历 _scope_files(含 readable)"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
