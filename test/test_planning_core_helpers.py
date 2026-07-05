#!/usr/bin/env python3
"""planning_core 叶 helper 行为契约锁（迁移安全网）。

_has_stream_stall / _git_diff_for_paths 此前无【直接】测试——只经被 patch 掉的路径间接触达。
本文件在【迁移前】锁住它们的可观测行为，使 god-file 拆解（抽 planning_core）后若漏依赖/漏
re-export 会立刻红。同时验证符号仍以 swarm.brain.nodes.X 可寻址（re-export 契约）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from swarm.brain.nodes import _git_diff_for_paths, _has_stream_stall
from swarm.types import WorkerOutput


# ── _has_stream_stall：失败详情含流式 stall 特征词 → True ────────────────────
def test_has_stream_stall_detects_marker_in_worker_output():
    out = WorkerOutput(subtask_id="st-1", diff="", summary="",
                       l1_passed=False, l1_details={"error": "stream stall timeout 首 token(prefill)"})
    assert _has_stream_stall({"st-1": out}, ["st-1"]) is True


def test_has_stream_stall_detects_marker_in_summary_dict_form():
    # dict 形态（非 WorkerOutput）+ 特征词落在 summary
    out = {"l1_details": {}, "summary": "解码中途 断流"}
    assert _has_stream_stall({"st-1": out}, ["st-1"]) is True


def test_has_stream_stall_false_when_no_marker():
    out = WorkerOutput(subtask_id="st-1", diff="", summary="普通编译失败",
                       l1_passed=False, l1_details={"error": "cannot find symbol"})
    assert _has_stream_stall({"st-1": out}, ["st-1"]) is False
    assert _has_stream_stall({}, []) is False
    assert _has_stream_stall({"st-1": out}, ["st-missing"]) is False  # id 不在结果里


# ── _git_diff_for_paths：据本地树现状为指定文件产 unified diff ────────────────
def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def test_git_diff_for_paths_new_untracked_file_produces_diff(tmp_path):
    repo = _git_repo(tmp_path)
    # 需要一个 base commit，git diff HEAD 才有参照
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    new = repo / "New.java"
    new.write_text("class New {}\n", encoding="utf-8")

    diff = _git_diff_for_paths(str(repo), ["New.java"])
    assert "New.java" in diff and "class New" in diff, "新建文件应出现在 diff（add -N intent-to-add）"
    # 产出后应撤销 intent-to-add（文件仍在，但不在暂存区）
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                            cwd=repo, capture_output=True, text=True).stdout
    assert "New.java" not in staged, "产出后应 git reset 撤销 intent-to-add"


def test_git_diff_for_paths_empty_paths_returns_empty(tmp_path):
    repo = _git_repo(tmp_path)
    assert _git_diff_for_paths(str(repo), []) == ""


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
