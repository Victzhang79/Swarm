"""F5（20_merge_flow_audit）：reset 链 best-effort 治本——失败文件清单 + 双向误判分流。

- `_reset_worktree_to_head` 返回失败清单（旧实现 per-file 非零不查、异常吞掉=半失败静默）。
- L2 pre-check：reset 半失败 → apply/编译结论不可信 → infra 降级（compile_unverified 同口径，
  不判代码失败、绝不假绿）。
- L2 功能测试（沙箱/本地）：本地脏树探测（sync 不带 .git，判据必须在本地 git）→ infra None。
- 交付临界区：reset 半失败 → fail-closed 不 apply 不 commit（防 commit 混入 pull-back 旧残留）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo_f5"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("# base a\n")
    (repo / "b.py").write_text("# base b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


_DIFF = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    "@@ -1 +1,2 @@\n# base a\n+# changed\n"
    "diff --git a/new.py b/new.py\nnew file mode 100644\n"
    "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+# new\n"
)


def test_reset_returns_empty_on_clean(tmp_path):
    """干净 reset：全部成功 → 空清单（新契约）。"""
    from swarm.brain.integration_review import _reset_worktree_to_head
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("# base a\n# DIRTY\n")
    (repo / "new.py").write_text("# residue\n")
    failed = _reset_worktree_to_head(str(repo), _DIFF)
    assert failed == []
    assert (repo / "a.py").read_text() == "# base a\n"
    assert not (repo / "new.py").exists()


def test_reset_records_per_file_failure(tmp_path, monkeypatch):
    """单文件 checkout 非零 → 只记该文件（半失败不再静默），其余照常被 reset。"""
    import swarm.brain.integration_review as ir
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("# base a\n# DIRTY\n")
    real_run = subprocess.run

    def _fake(cmd, **kw):
        if "checkout" in cmd and "a.py" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "locked")
        return real_run(cmd, **kw)

    monkeypatch.setattr(ir.subprocess, "run", _fake)
    failed = ir._reset_worktree_to_head(str(repo), _DIFF)
    assert failed == ["a.py"]
    assert not (repo / "new.py").exists(), "新建残留删除不受他文件失败连坐"


def test_reset_exception_returns_all_files_failclosed(tmp_path, monkeypatch):
    """整段异常=任意文件状态未知 → 全量记失败（fail-closed 宁多勿漏）。"""
    import swarm.brain.integration_review as ir
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(ir, "files_from_unified_diff",
                        lambda _d: (_ for _ in ()).throw(RuntimeError("boom")))
    failed = ir._reset_worktree_to_head(str(repo), _DIFF)
    assert failed == ["<reset-exception>"]


def test_untracked_new_file_not_misreported(tmp_path):
    """未 add 过的新文件 `git rm --cached` 恒非零——不得误报半失败（先查 index 再撤）。"""
    from swarm.brain.integration_review import _reset_worktree_to_head
    repo = _make_repo(tmp_path)
    (repo / "new.py").write_text("# residue\n")  # untracked，从未 add
    failed = _reset_worktree_to_head(str(repo), _DIFF)
    assert failed == []
    assert not (repo / "new.py").exists()


def test_worktree_phase_reset_failure_infra_degrade(tmp_path, monkeypatch):
    """L2 pre-check reset 半失败 → infra 降级：compile_unverified 口径，issue 标 infra
    （不判代码失败），绝不进 apply。"""
    import swarm.brain.integration_review as ir
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(ir, "_reset_worktree_to_head", lambda *a, **k: ["a.py"])
    monkeypatch.setattr(ir, "apply_git_diff",
                        lambda *a, **k: pytest.fail("reset 半失败时绝不应进 apply"))
    passed, issues, details = ir._run_worktree_phase(
        str(repo), _DIFF, {}, [], timeout=10, compile_runner=None, base_ref=None)
    assert passed is False
    assert details["compile_unverified"] is True and details["compile_ok"] is None
    assert details["reset_failed_files"] == ["a.py"]
    assert any("infra" in i for i in issues), "失败必须标 infra 口径（非代码失败归因）"


def test_l2_tree_dirty_detects_residue(tmp_path):
    """本地脏树探测：merged_diff 涉及文件有未提交改动 → 清单非空；干净 → 空。"""
    from swarm.brain.nodes import _l2_tree_dirty
    repo = _make_repo(tmp_path)
    assert _l2_tree_dirty(str(repo), _DIFF) == []
    (repo / "a.py").write_text("# base a\n# residue\n")
    assert _l2_tree_dirty(str(repo), _DIFF) == ["a.py"]


def test_try_l2_sandbox_verify_dirty_tree_infra_none(tmp_path, monkeypatch):
    """脏树 → 沙箱 L2 infra None（不判代码失败），且绝不创建沙箱。"""
    import swarm.brain.nodes as nodes
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("# base a\n# residue\n")
    monkeypatch.setattr(nodes, "_sandbox_available", lambda: True)
    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: str(repo))
    monkeypatch.setattr(nodes, "_run_l2_in_sandbox",
                        lambda *a, **k: pytest.fail("脏树时绝不应进沙箱"))
    assert nodes._try_l2_sandbox_verify("pid", _DIFF, "pytest -q") is None


def test_deliver_reset_failure_fail_closed_no_commit(tmp_path, monkeypatch):
    """交付临界区：reset 半失败 → fail-closed 不 apply 不 commit（防毒树入库）。"""
    import swarm.brain.integration_review as ir
    from swarm.brain.nodes import _deliver_merged_diff_locked
    repo = _make_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(ir, "_reset_worktree_to_head", lambda *a, **k: ["a.py"])
    result = _deliver_merged_diff_locked(str(repo), _DIFF, None, ["a.py"], "t-1")
    assert result["ap"]["ok"] is False
    assert result["ap"]["stage"] == "reset_partial_failure"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before, \
        "reset 半失败时绝不产生 commit（防交付混入旧残留）"
    assert (repo / "a.py").read_text() == "# base a\n", "fail-closed 不得动工作区"


def test_rollback_reset_failure_flips_passed(tmp_path, monkeypatch):
    """闸门 R2（reviewer MEDIUM②/hunter LOW-6）：rollback 半失败必须翻转 passed——
    只入 details 时无 test_cmd 的任务 L2 假绿且脏树留共享工作区（M-3 家族污染源）。"""
    import swarm.brain.integration_review as ir
    repo = _make_repo(tmp_path)
    (repo / "package.json").write_text('{"name":"t","scripts":{"build":"true"}}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pkg")
    # _DIFF 上下文行缺前导空格（reset 测试够用但 git apply 不接受）——本测试走完整
    # run_integration_review，必须用合法补丁。
    good_diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1,2 @@\n # base a\n+# changed\n"
        "diff --git a/new.py b/new.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+# new\n"
    )
    calls = {"n": 0}

    def _flaky(*a, **k):  # 首次（入场 pre-check）干净，rollback 半失败
        calls["n"] += 1
        return [] if calls["n"] == 1 else ["a.py"]

    monkeypatch.setattr(ir, "_reset_worktree_to_head", _flaky)
    ok, issues, details = ir.run_integration_review(
        str(repo), good_diff, None, compile_runner=lambda cmd: (True, True, "ok"))
    assert ok is False, "rollback 半失败绝不许 passed=True（假绿）"
    assert details.get("rollback_reset_failed") == ["a.py"]
    assert any("回滚半失败" in i for i in issues), "infra 降级必须进 issues 翻转 passed"


def test_l2_rollback_reset_failed_never_attributed(tmp_path, monkeypatch):
    """闸门 R2 对称面：rollback 半失败走 verify_l2 时同样不归因不定项（issue 文本携
    残留文件路径，归因会误命中写者子任务——与 compile_unverified 同病同药）。"""
    import asyncio

    import swarm.brain.nodes as nodes
    from swarm.brain.nodes import verify as v
    from swarm.types import (FileScope, SubTask, SubTaskDifficulty, SubTaskModality,
                             TaskPlan)

    repo = _make_repo(tmp_path)
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="d",
                          difficulty=SubTaskDifficulty.MEDIUM,
                          modality=SubTaskModality.TEXT,
                          scope=FileScope(writable=["a.py"]))],
        parallel_groups=[["st-1"]],
    )
    state = {
        "project_id": "proj-f5rb",
        "merged_diff": _DIFF,
        "task_description": "d",
        "plan": plan,
        "subtask_results": {"st-1": {"status": "success"}},
    }
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: str(repo))
    monkeypatch.setattr(
        "swarm.brain.integration_review.run_integration_review",
        lambda *a, **k: (False, ["L2 回滚半失败（工作区残留）: a.py ——infra 降级"],
                         {"compile_ok": True, "rollback_reset_failed": ["a.py"]}))
    monkeypatch.setattr(v, "attribute_l2_failure",
                        lambda *a, **k: pytest.fail("infra 降级绝不许归因定向"))

    out = asyncio.run(v.verify_l2(state))
    assert out.get("l2_passed") is False
    assert out.get("failure_strategy") == "replan"
    assert "l2_targeted" not in out
    assert out.get("failed_subtask_ids") == ["st-1"]
    assert out["l2_details"]["infra_degrade"] == "rollback_reset_failed"


def test_l2_compile_unverified_never_attributed(tmp_path, monkeypatch):
    """批次7 复核 R1 整改①：F5 infra 降级（compile_unverified）绝不走 attribute_l2_failure
    定向——issue 文本携带残留文件路径，归因会误命中其写者子任务，把 infra 伪装成代码失败
    定向重试无辜者。走既有"归因不出"口径：无 l2_targeted、连坐全部、replan。"""
    import asyncio

    import swarm.brain.nodes as nodes
    from swarm.brain.nodes import verify as v
    from swarm.types import (FileScope, SubTask, SubTaskDifficulty, SubTaskModality,
                             TaskPlan)

    repo = _make_repo(tmp_path)
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="d",
                          difficulty=SubTaskDifficulty.MEDIUM,
                          modality=SubTaskModality.TEXT,
                          scope=FileScope(writable=["a.py"]))],
        parallel_groups=[["st-1"]],
    )
    state = {
        "project_id": "proj-f5",
        "merged_diff": _DIFF,
        "task_description": "d",
        "plan": plan,
        "subtask_results": {"st-1": {"status": "success"}},
    }
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: str(repo))
    monkeypatch.setattr(
        "swarm.brain.integration_review.run_integration_review",
        lambda *a, **k: (False, ["L2 infra：reset 半失败残留 a.py"],
                         {"compile_unverified": True, "reset_failed_files": ["a.py"]}))

    def _tripwire(*a, **k):  # 归因被调用=整改失效
        pytest.fail("compile_unverified 是 infra 降级，绝不许走 attribute_l2_failure 定向")

    monkeypatch.setattr(v, "attribute_l2_failure", _tripwire)

    out = asyncio.run(v.verify_l2(state))
    assert out.get("l2_passed") is False
    assert out.get("failure_strategy") == "replan"
    assert "l2_targeted" not in out, "infra 降级绝不打定向重试标记"
    assert out.get("failed_subtask_ids") == ["st-1"], "归因不出→连坐全部口径"
    assert out["l2_details"]["infra_degrade"] == "compile_unverified"
