#!/usr/bin/env python3
"""TD2606-C9 回归：L1 闸门在沙箱里确定性修复的文件（含子任务写权 scope 之外的，如父 pom）
必须回传本地 + 计入 diff，杜绝"本地 diff/scope"与"沙箱 compile/test"两棵真值树静默分叉。

核心：_record_repaired_paths 累积修复路径，_get_git_diff/_sync_from_sandbox 把它们一并纳入，
即便文件不在 writable/create scope 内。否则父 pom 的版本号修复只活在沙箱、merged_diff 缺失，
brain 端 L2 集成构建在干净树上重炸。
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope, SubTask
from swarm.worker.executor import WorkerExecutor


def _executor(tmp_path, writable=None, create_files=None):
    st = SubTask(
        id="sub-1",
        description="测试子任务",
        scope=FileScope(
            writable=writable if writable is not None else [],
            create_files=create_files or [],
        ),
    )
    return WorkerExecutor(st, project_path=str(tmp_path), project_id="p1", task_id="t1")


def _git(tmp_path, *args):
    subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                   capture_output=True, text=True)


# ── _record_repaired_paths ──

def test_record_repaired_paths_accumulates_and_normalizes(tmp_path):
    ex = _executor(tmp_path, writable=["mod/Foo.java"])
    ex._record_repaired_paths({"repaired_file_paths": ["./pom.xml", "mod/Foo.java"]})
    # 去 ./ 前缀；含 scope 外的 pom.xml
    assert "pom.xml" in ex._repaired_extra_paths
    assert "mod/Foo.java" in ex._repaired_extra_paths


def test_record_repaired_paths_noop_without_paths(tmp_path):
    ex = _executor(tmp_path, writable=["a.py"])
    ex._record_repaired_paths({"import_repaired_files": 3})  # 只有计数无路径
    assert ex._repaired_extra_paths == set()


# ── _get_git_diff：scope 外被修复文件进入 diff ──

def test_git_diff_includes_out_of_scope_repaired_file(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    # 父 pom（子任务写权之外）+ 子任务可写的源文件，先提交为干净基线
    (tmp_path / "pom.xml").write_text("<project><version>1.0.0</version></project>\n")
    (tmp_path / "mod").mkdir()
    (tmp_path / "mod" / "Foo.java").write_text("class Foo {}\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")

    # 模拟 pull-back 后的工作区：源文件改了（in scope），父 pom 版本号被 version-repair 改了（out of scope）
    (tmp_path / "mod" / "Foo.java").write_text("class Foo { int x; }\n")
    (tmp_path / "pom.xml").write_text("<project><version>1.0.1</version></project>\n")

    ex = _executor(tmp_path, writable=["mod/Foo.java"])

    # 控制组：未登记修复 → diff 只含 scope 内文件，漏掉父 pom（这正是 C9 的 bug）
    baseline = ex._get_git_diff()
    assert "mod/Foo.java" in baseline
    assert "pom.xml" not in baseline, "未登记时 scope 外文件本就不在 diff（复现 bug 前提）"

    # 修复后：登记父 pom → diff 必须把它带进来
    ex._record_repaired_paths({"repaired_file_paths": ["pom.xml"]})
    diff = ex._get_git_diff()
    assert "mod/Foo.java" in diff
    assert "pom.xml" in diff, "被修复的 scope 外父 pom 必须进 merged_diff，否则集成重炸"
    assert "1.0.1" in diff


def test_sync_from_sandbox_local_mode_snapshots_repaired(tmp_path):
    """本地模式 pull-back 快照应包含被修复的 scope 外文件（供 difflib 路径产出 diff）。"""
    import asyncio

    (tmp_path / "pom.xml").write_text("<project><version>1.0.1</version></project>\n")
    ex = _executor(tmp_path, writable=["a.py"])
    (tmp_path / "a.py").write_text("x = 1\n")
    ex._record_repaired_paths({"repaired_file_paths": ["pom.xml"]})
    # 无沙箱 → 本地模式分支
    asyncio.run(ex._sync_from_sandbox("test"))
    assert "pom.xml" in ex._post_sync_contents
    assert "1.0.1" in (ex._post_sync_contents["pom.xml"] or "")
