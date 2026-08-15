#!/usr/bin/env python3
"""B6 复核 #2 回归：contract_utils._exists_in_repo 以钉扎 base 为基线，非实时 HEAD。

ELABORATE 在 replan/resplit 重跑时 HEAD 可能已被推进；读 HEAD 会把"base 时新建、HEAD 时已存在"
的文件误判 aggregate，与 merge/worker 全链 base 混基线。
"""

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
    (repo / "base.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_exists_in_repo_uses_pinned_base_not_moved_head(tmp_path):
    from swarm.brain.contract_utils import _exists_in_repo

    repo = _mkrepo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    # 运行期 HEAD 前移：新增 newmod/pom.xml 并提交（base 时不存在，HEAD 时存在）
    (repo / "newmod").mkdir()
    (repo / "newmod" / "pom.xml").write_text("<p/>\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add newmod")

    # 钉扎 base → 该文件在 base 【不存在】→ 判为新建撞车（False），不误当 aggregate
    assert _exists_in_repo(str(repo), "newmod/pom.xml", {}, base) is False
    # 不传 base（HEAD 行为）→ 已存在（True）——正是要避免的混基线
    assert _exists_in_repo(str(repo), "newmod/pom.xml", {}, None) is True
    # base 时就有的文件 → base 下仍 True
    assert _exists_in_repo(str(repo), "base.txt", {}, base) is True


def test_elaborate_threads_base_ref():
    """★批25 GS-5w 换锁★（原命题：elaborate 源码含 'base_ref=state.get("base_commit")'
    调用形式——getsource 字面量断言；现改为行为锁）：spy resolve_plan_conflicts，
    真跑 elaborate，断它真收到 base_ref=state["base_commit"]（B6 #2：钉扎 base 非实时
    HEAD——上面 _exists_in_repo 的真 git 行为测守【消费侧】，本条守【透传侧】）。
    区分力：删掉/改坏 elaborate 调用点的 base_ref kwarg → spy 收不到哨兵值 → 红；
    resolve_plan_conflicts 不再被 elaborate 调用 → 红。"""
    import asyncio
    from unittest.mock import patch

    from swarm.brain import contract_utils, planning_nodes
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="改 a",
                scope=FileScope(writable=["a.java"], readable=[])),
    ])
    seen: dict = {}
    real = contract_utils.resolve_plan_conflicts

    def _spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    with patch.object(contract_utils, "resolve_plan_conflicts", _spy):
        asyncio.run(planning_nodes.elaborate(
            {"plan": plan, "task_id": "", "project_id": "",
             "base_commit": "base-pin-sentinel"}))

    assert seen, "夹具前提失效：elaborate 未调用 resolve_plan_conflicts"
    assert seen.get("base_ref") == "base-pin-sentinel", (
        f"elaborate 必须把 state.base_commit 透传为 base_ref（B6 #2 钉扎 base），"
        f"实际 kwargs: {sorted(seen)} base_ref={seen.get('base_ref')!r}")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
