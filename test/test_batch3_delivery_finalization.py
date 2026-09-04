"""第三批：交付 finalizer 新发现失败必须覆盖先前人工接受。"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

import swarm.brain.nodes as nodes
import swarm.brain.runner as runner
from swarm.brain.delivery_finalize import deliver_merged_diff_locked
from swarm.brain.gates import delivery_outcome
from swarm.types import Complexity, HumanDecision


def test_finalization_failure_overrides_prior_partial_review_at_runner(monkeypatch):
    async def _deliver(*_args):
        return {
            "ap": {"ok": False, "applied": [], "failed": ["a.py"]},
            "out_files": [],
            "wm": {},
            "commit": {"ok": True, "committed": False},
        }

    async def _persist_failure(_state, _parsed):
        return {"persisted": True}

    class _FailureLLM:
        async def ainvoke(self, _messages):
            return type("Response", (), {"content": "{}"})()

    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: "/project")
    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _deliver)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FailureLLM())
    monkeypatch.setattr("swarm.brain.learn_store.persist_learn_failure", _persist_failure)

    before = {
        "plan_valid": True,
        "l2_passed": True,
        "l3_passed": True,
        "runtime_smoke_passed": True,
        "acceptance_passed": True,
        "failed_subtask_ids": [],
        "failure_escalated": False,
        "requirement_denominator_complete": True,
        "task_id": "t-reviewed-then-failed",
        "project_id": "p",
        "task_description": "partial accepted",
        "complexity": Complexity.SIMPLE,
        "merged_diff": "diff --git a/a.py b/a.py\n+x\n",
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
        "abandoned_subtask_ids": ["st-missing"],
        "plan": {"subtasks": [{"id": "st-good"}, {"id": "st-missing"}]},
        "subtask_results": {"st-good": {"l1_passed": True}},
    }
    learned_patch = asyncio.run(nodes.learn_success(before))
    terminal_state = {**before, **learned_patch}

    writes: list[dict] = []

    async def _emit(_queue, _event):
        return None

    monkeypatch.setattr(runner, "_emit", _emit)
    monkeypatch.setattr(runner, "_sync_task_from_state", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_sweep_unverified_footprints", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_emit_task_notification", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_failed_machine_account", lambda *_a, **_k: {})
    monkeypatch.setattr(runner, "audit", lambda *_a, **_k: None)
    monkeypatch.setattr(runner.store, "get_task", lambda tid: {
        "id": tid, "project_id": "p", "description": "partial accepted",
    })
    monkeypatch.setattr(
        runner.store,
        "update_task",
        lambda tid, **kwargs: writes.append(kwargs) or {"id": tid, **kwargs},
    )

    asyncio.run(runner._handle_post_run("t-reviewed-then-failed", terminal_state, None))

    assert delivery_outcome(terminal_state) == "FAILED"
    assert [row["status"] for row in writes if row.get("status")] == ["FAILED"]


def test_commit_observation_warning_is_consumed_and_blocks_success_learning(monkeypatch):
    captured: list[dict] = []

    async def _deliver(*_args):
        return {
            "ap": {"ok": True, "applied": ["a.py"], "failed": []},
            "out_files": ["a.py"],
            "wm": {},
            "commit": {
                "ok": True,
                "committed": True,
                "commit_hash": "",
                "observation_warning": "commit 已成功，但读取 HEAD 失败",
            },
        }

    async def _persist(state, _parsed):
        captured.append(state)
        return {"persisted": False, "reason": "blocked"}

    monkeypatch.setattr(nodes, "_get_project_path", lambda _pid: "/project")
    monkeypatch.setattr(nodes, "_deliver_merged_diff_serialized", _deliver)
    monkeypatch.setattr("swarm.brain.learn_store.persist_learn_success", _persist)
    monkeypatch.setattr(
        "swarm.knowledge.hooks.schedule_incremental_update", lambda *_a, **_k: None
    )

    out = asyncio.run(nodes.learn_success({
        "task_id": "t-observation-warning",
        "project_id": "p",
        "task_description": "修改 a.py",
        "complexity": Complexity.SIMPLE,
        "merged_diff": _DIFF,
        "human_decision": HumanDecision.ACCEPT,
        "delivery_reviewed": True,
        "plan_valid": True,
        "l2_passed": True,
        "runtime_smoke_passed": True,
        "l3_passed": True,
        "acceptance_passed": True,
        "requirement_denominator_complete": True,
        "failed_subtask_ids": [],
        "failure_escalated": False,
        "plan": {"subtasks": [{"id": "st-1"}]},
        "subtask_results": {"st-1": {"l1_passed": True}},
    }))

    assert "delivery_commit_observation_warning" in out["degraded_reasons"]
    assert "delivery_commit_observation_warning" in captured[0]["degraded_reasons"]


def test_manifest_reconcile_error_stops_before_commit_and_rolls_back(monkeypatch, tmp_path):
    calls = {"commit": 0, "reset": 0}
    monkeypatch.setattr("swarm.worker.executor._ProjectGitFlock", lambda _p: _NullContext())
    monkeypatch.setattr("subprocess.run", _successful_git_probe)
    monkeypatch.setattr(
        "swarm.brain.delivery_finalize._capture_git_index",
        lambda _p: (tmp_path / "index", None, 0),
    )
    monkeypatch.setattr("swarm.git_base.files_changed_since_base", lambda *_a, **_k: [])
    monkeypatch.setattr("swarm.git_base.uncommitted_changed_files", lambda *_a, **_k: [])
    monkeypatch.setattr("swarm.project.diff_apply.apply_git_diff_resilient", lambda *_a: {
        "ok": True, "applied": ["a.py"], "failed": [],
    })

    def _reset(*_a):
        calls["reset"] += 1
        return []

    monkeypatch.setattr("swarm.brain.integration_review._reset_worktree_to_head", _reset)
    monkeypatch.setattr("swarm.brain.integration_review.restore_worktree_to_diff_baseline", _reset)
    monkeypatch.setattr("swarm.worker.workspace_manifest.reconcile_workspace_manifests", lambda *_a: {
        "modified_manifests": ["pom.xml"],
        "added": {},
        "removed": {},
        "reconcile_errors": {"maven": "parse failed"},
    })
    monkeypatch.setattr("swarm.project.diff_apply.commit_task_output", lambda *_a, **_k: (
        calls.__setitem__("commit", calls["commit"] + 1) or {"ok": True}
    ))

    out = deliver_merged_diff_locked(
        str(tmp_path), _DIFF, "base", ["a.py"], "t-manifest-error"
    )

    assert calls["commit"] == 0
    assert calls["reset"] == 2, "落盘前一次 reset；清单失败后必须再回滚"
    assert out["finalization_error"] == "manifest_reconcile_failed"
    assert out["ap"]["ok"] is False


def test_commit_failure_rolls_back_applied_tree(monkeypatch, tmp_path):
    calls = {"reset": 0}
    monkeypatch.setattr("swarm.worker.executor._ProjectGitFlock", lambda _p: _NullContext())
    monkeypatch.setattr("subprocess.run", _successful_git_probe)
    monkeypatch.setattr(
        "swarm.brain.delivery_finalize._capture_git_index",
        lambda _p: (tmp_path / "index", None, 0),
    )
    monkeypatch.setattr("swarm.git_base.files_changed_since_base", lambda *_a, **_k: [])
    monkeypatch.setattr("swarm.git_base.uncommitted_changed_files", lambda *_a, **_k: [])
    monkeypatch.setattr("swarm.project.diff_apply.apply_git_diff_resilient", lambda *_a: {
        "ok": True, "applied": ["a.py"], "failed": [],
    })

    def _reset(*_a):
        calls["reset"] += 1
        return []

    monkeypatch.setattr("swarm.brain.integration_review._reset_worktree_to_head", _reset)
    monkeypatch.setattr("swarm.brain.integration_review.restore_worktree_to_diff_baseline", _reset)
    monkeypatch.setattr("swarm.worker.workspace_manifest.reconcile_workspace_manifests", lambda *_a: {
        "modified_manifests": [], "added": {}, "removed": {}, "reconcile_errors": {},
    })
    monkeypatch.setattr("swarm.project.diff_apply.commit_task_output", lambda *_a, **_k: {
        "ok": False, "committed": False, "reason": "git commit failed",
    })

    out = deliver_merged_diff_locked(
        str(tmp_path), _DIFF, "base", ["a.py"], "t-commit-error"
    )

    assert calls["reset"] == 2
    assert out["finalization_error"] == "commit_failed"
    assert out["ap"]["ok"] is False


def test_commit_failure_rolls_back_already_present_worker_tree(monkeypatch, tmp_path):
    """worker pull-back 已呈现期望内容不等于已提交；commit 失败不能留下 staged/dirty。"""
    calls = {"reset": 0}
    monkeypatch.setattr("swarm.worker.executor._ProjectGitFlock", lambda _p: _NullContext())
    monkeypatch.setattr("subprocess.run", _successful_git_probe)
    monkeypatch.setattr(
        "swarm.brain.delivery_finalize._capture_git_index",
        lambda _p: (tmp_path / "index", None, 0),
    )
    monkeypatch.setattr("swarm.git_base.files_changed_since_base", lambda *_a, **_k: [])
    monkeypatch.setattr("swarm.git_base.uncommitted_changed_files", lambda *_a, **_k: ["a.py"])
    monkeypatch.setattr(
        "swarm.brain.integration_review.worktree_matches_merged_diff",
        lambda *_a: (True, []),
    )

    def _reset(*_a):
        calls["reset"] += 1
        return []

    monkeypatch.setattr("swarm.brain.integration_review._reset_worktree_to_head", _reset)
    monkeypatch.setattr("swarm.brain.integration_review.restore_worktree_to_diff_baseline", _reset)
    monkeypatch.setattr("swarm.worker.workspace_manifest.reconcile_workspace_manifests", lambda *_a: {
        "modified_manifests": [], "added": {}, "removed": {}, "reconcile_errors": {},
    })
    monkeypatch.setattr("swarm.project.diff_apply.commit_task_output", lambda *_a, **_k: {
        "ok": False, "committed": False, "reason": "failed after git add",
    })

    out = deliver_merged_diff_locked(
        str(tmp_path), _DIFF, "base", ["a.py"], "t-worker-tree"
    )

    assert calls["reset"] == 1
    assert out["finalization_error"] == "commit_failed"
    assert out["ap"]["rollback_failed"] == []


def test_monorepo_subproject_commit_failure_restores_prefixed_base(monkeypatch, tmp_path):
    """子项目 diff 坐标相对 sub/，回滚 cat-file 必须补仓根 prefix。"""
    repo = tmp_path / "repo"
    project = repo / "sub"
    project.mkdir(parents=True)
    target = project / "a.py"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "sub/a.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    target.write_text("new\n", encoding="utf-8")
    merged_diff = subprocess.run(
        ["git", "-C", str(project), "diff", "--relative", "--", "a.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    def _fail_after_add(project_path, files, **_kwargs):
        subprocess.run(
            ["git", "-C", project_path, "add", "--", *files], check=True
        )
        return {"ok": False, "committed": False, "reason": "forced"}

    monkeypatch.setattr("swarm.project.diff_apply.commit_task_output", _fail_after_add)

    out = deliver_merged_diff_locked(
        str(project), merged_diff, base, ["a.py"], "t-monorepo-rollback"
    )

    assert out["finalization_error"] == "commit_failed"
    assert out["ap"]["rollback_failed"] == []
    assert target.read_text(encoding="utf-8") == "old\n"
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""


def test_non_git_manifest_failure_restores_pre_delivery_tree(monkeypatch, tmp_path):
    target = tmp_path / "a.py"
    manifest = tmp_path / "pom.xml"
    target.write_text("new\n", encoding="utf-8")
    original_manifest = "<project><modules></modules></project>\n"
    manifest.write_text(original_manifest, encoding="utf-8")

    def _manifest_error(project_path):
        (type(manifest)(project_path) / "pom.xml").write_text("<new/>\n", encoding="utf-8")
        return {
            "modified_manifests": ["pom.xml"],
            "added": {}, "removed": {}, "reconcile_errors": {"maven": "forced"},
        }

    monkeypatch.setattr(
        "swarm.worker.workspace_manifest.reconcile_workspace_manifests", _manifest_error
    )

    out = deliver_merged_diff_locked(
        str(tmp_path), _DIFF, None, ["a.py"], "t-non-git"
    )

    assert out["finalization_error"] == "manifest_reconcile_failed"
    assert out["ap"]["rollback_failed"] == []
    assert target.read_text(encoding="utf-8") == "old\n"
    assert manifest.read_text(encoding="utf-8") == original_manifest


def test_false_commit_result_after_real_commit_is_recovered(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path, {"a.py": "old\n"})
    base = _head(repo)

    def _commit_then_report_failure(project_path, files, **_kwargs):
        subprocess.run(["git", "-C", project_path, "add", "--", *files], check=True)
        subprocess.run(
            ["git", "-C", project_path, "-c", "user.name=test", "-c", "user.email=t@t",
             "commit", "-m", "actually committed"],
            check=True,
            capture_output=True,
        )
        return {"ok": False, "committed": False, "reason": "post-commit probe failed"}

    monkeypatch.setattr("swarm.project.diff_apply.commit_task_output", _commit_then_report_failure)

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-post-commit")

    assert out["commit"]["ok"] is True
    assert out["commit"]["committed"] is True
    assert out["commit"]["observation_recovered"] is True
    assert (repo / "a.py").read_text(encoding="utf-8") == "new\n"
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == ""


def test_unrelated_head_advance_cannot_recover_failed_task_commit(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path, {"a.py": "old\n", "b.py": "base\n"})
    base = _head(repo)

    def _commit_unrelated_then_report_failure(project_path, _files, **_kwargs):
        (repo / "b.py").write_text("unrelated\n", encoding="utf-8")
        subprocess.run(["git", "-C", project_path, "add", "--", "b.py"], check=True)
        subprocess.run(
            ["git", "-C", project_path, "-c", "user.name=test", "-c", "user.email=t@t",
             "commit", "--only", "-m", "unrelated", "--", "b.py"],
            check=True,
            capture_output=True,
        )
        return {"ok": False, "committed": False, "reason": "task commit failed"}

    monkeypatch.setattr(
        "swarm.project.diff_apply.commit_task_output",
        _commit_unrelated_then_report_failure,
    )

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-unrelated")

    assert out["finalization_error"] == "commit_failed"
    assert (repo / "a.py").read_text(encoding="utf-8") == "old\n"
    assert (repo / "b.py").read_text(encoding="utf-8") == "unrelated\n"
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == ""


def test_dirty_manifest_is_rejected_before_apply_or_commit(monkeypatch, tmp_path):
    repo = _init_repo(
        tmp_path,
        {"pom.xml": "<project><modules></modules></project>\n"},
    )
    base = _head(repo)
    (repo / "pom.xml").write_text(
        "<project><modules></modules><!-- USER EDIT --></project>\n",
        encoding="utf-8",
    )
    child_diff = """diff --git a/child/pom.xml b/child/pom.xml
new file mode 100644
--- /dev/null
+++ b/child/pom.xml
@@ -0,0 +1 @@
+<project/>
"""
    monkeypatch.setattr(
        "swarm.project.diff_apply.commit_task_output",
        lambda *_a, **_k: pytest.fail("manifest 冲突必须在 commit 前拒绝"),
    )

    out = deliver_merged_diff_locked(
        str(repo), child_diff, base, ["child/pom.xml"], "t-dirty-manifest"
    )

    assert out["ap"]["stage"] == "manifest_worktree_conflict"
    assert "USER EDIT" in (repo / "pom.xml").read_text(encoding="utf-8")
    assert not (repo / "child/pom.xml").exists()


def test_dirty_leaf_package_manifest_does_not_block_unrelated_delivery(tmp_path):
    repo = _init_repo(tmp_path, {
        "a.py": "old\n",
        "packages/x/package.json": '{"name":"x"}\n',
    })
    base = _head(repo)
    leaf = repo / "packages/x/package.json"
    leaf.write_text('{"name":"x","private":true}\n', encoding="utf-8")

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-leaf-dirty")

    assert out["commit"]["ok"] is True
    assert out["commit"]["committed"] is True
    assert (repo / "a.py").read_text(encoding="utf-8") == "new\n"
    assert '"private":true' in leaf.read_text(encoding="utf-8")
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "packages/x/package.json" in status


def test_symlinked_manifest_commits_the_real_in_repo_target(tmp_path):
    repo = _init_repo(tmp_path, {
        "a.py": "old\n",
        "real/root-package.json": '{"private":true,"workspaces":["packages/a"]}\n',
        "packages/a/package.json": '{"name":"a"}\n',
        "packages/x/package.json": '{"name":"x"}\n',
    })
    (repo / "package.json").symlink_to("real/root-package.json")
    subprocess.run(["git", "-C", str(repo), "add", "package.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", "add manifest symlink"],
        check=True, capture_output=True,
    )
    base = _head(repo)

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-symlink")

    assert out["commit"]["ok"] is True
    assert out["commit"]["committed"] is True
    committed_manifest = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD:real/root-package.json"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "packages/x" in committed_manifest
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == ""


def test_manifest_symlink_outside_project_fails_before_any_write(tmp_path):
    repo = _init_repo(tmp_path, {
        "a.py": "old\n",
        "packages/a/package.json": '{"name":"a"}\n',
        "packages/x/package.json": '{"name":"x"}\n',
    })
    outside = tmp_path / "outside-package.json"
    outside.write_text(
        '{"private":true,"workspaces":["packages/a"]}\n', encoding="utf-8"
    )
    (repo / "package.json").symlink_to(outside)
    subprocess.run(["git", "-C", str(repo), "add", "package.json"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", "add external manifest symlink"],
        check=True, capture_output=True,
    )
    base = _head(repo)

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-external")

    assert out["ap"]["stage"] == "manifest_symlink_outside_project"
    assert (repo / "a.py").read_text(encoding="utf-8") == "old\n"
    assert outside.read_text(encoding="utf-8") == (
        '{"private":true,"workspaces":["packages/a"]}\n'
    )
    assert _head(repo) == base


def test_workspace_reconcile_never_traverses_directory_symlinks(tmp_path):
    repo = _init_repo(tmp_path, {"a.py": "old\n"})
    outside = tmp_path / "external-workspace"
    child = outside / "child"
    child.mkdir(parents=True)
    aggregate = outside / "pom.xml"
    aggregate.write_text(
        "<project><modules></modules></project>\n", encoding="utf-8"
    )
    (child / "pom.xml").write_text(
        "<project><parent></parent></project>\n", encoding="utf-8"
    )
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    subprocess.run(["git", "-C", str(repo), "add", "linked"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", "add directory symlink"],
        check=True, capture_output=True,
    )
    base = _head(repo)

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-dir-link")

    assert out["commit"]["ok"] is True
    assert out["commit"]["committed"] is True
    assert aggregate.read_text(encoding="utf-8") == "<project><modules></modules></project>\n"
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == ""


def test_dirty_manifest_under_workspace_skip_dir_does_not_block_delivery(tmp_path):
    repo = _init_repo(tmp_path, {
        "a.py": "old\n",
        "vendor/lib/pom.xml": "<project/>\n",
    })
    base = _head(repo)
    vendor_manifest = repo / "vendor/lib/pom.xml"
    vendor_manifest.write_text("<project><!-- user edit --></project>\n", encoding="utf-8")

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-vendor")

    assert out["commit"]["ok"] is True
    assert out["commit"]["committed"] is True
    assert "user edit" in vendor_manifest.read_text(encoding="utf-8")
    assert "vendor/lib/pom.xml" in subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_dirty_leaf_maven_pom_does_not_block_unrelated_delivery(tmp_path):
    repo = _init_repo(tmp_path, {
        "a.py": "old\n",
        "pom.xml": "<project><modules><module>child</module></modules></project>\n",
        "child/pom.xml": "<project><parent></parent></project>\n",
    })
    base = _head(repo)
    leaf = repo / "child/pom.xml"
    leaf.write_text("<project><parent></parent><!-- user edit --></project>\n", encoding="utf-8")

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-leaf-pom")

    assert out["commit"]["ok"] is True
    assert out["commit"]["committed"] is True
    assert "user edit" in leaf.read_text(encoding="utf-8")
    assert "child/pom.xml" in subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_dirty_nested_maven_aggregator_still_blocks_delivery(tmp_path):
    repo = _init_repo(tmp_path, {
        "a.py": "old\n",
        "pom.xml": "<project><modules><module>group</module></modules></project>\n",
        "group/pom.xml": "<project><parent></parent><modules></modules></project>\n",
    })
    base = _head(repo)
    nested = repo / "group/pom.xml"
    nested.write_text(
        "<project><parent></parent><modules></modules><!-- user edit --></project>\n",
        encoding="utf-8",
    )

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-nested-pom")

    assert out["ap"]["stage"] == "manifest_worktree_conflict"
    assert "group/pom.xml" in out["ap"]["failed"]
    assert (repo / "a.py").read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize(
    "manifest, original, edited",
    [
        ("package.json", '{"name":"leaf"}\n', '{"name":"leaf","private":true}\n'),
        ("Cargo.toml", '[package]\nname="leaf"\n', '[package]\nname="leaf"\nversion="1.0.0"\n'),
    ],
)
def test_dirty_non_workspace_root_manifest_does_not_block_delivery(
    tmp_path, manifest, original, edited,
):
    repo = _init_repo(tmp_path, {"a.py": "old\n", manifest: original})
    base = _head(repo)
    target = repo / manifest
    target.write_text(edited, encoding="utf-8")

    out = deliver_merged_diff_locked(str(repo), _DIFF, base, ["a.py"], "t-single-package")

    assert out["commit"]["ok"] is True
    assert out["commit"]["committed"] is True
    assert target.read_text(encoding="utf-8") == edited
    assert manifest in subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_git_index_snapshot_failure_stops_before_writes(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path, {"a.py": "old\n"})
    monkeypatch.setattr("swarm.brain.delivery_finalize._capture_git_index", lambda _p: None)
    monkeypatch.setattr(
        "swarm.project.diff_apply.apply_git_diff_resilient",
        lambda *_a: pytest.fail("index 无前像时不得 apply"),
    )

    out = deliver_merged_diff_locked(
        str(repo), _DIFF, _head(repo), ["a.py"], "t-index-snapshot"
    )

    assert out["ap"]["stage"] == "git_index_snapshot_failed"
    assert (repo / "a.py").read_text(encoding="utf-8") == "old\n"


def test_mixed_committed_and_worker_dirty_rollback_is_per_file(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path, {"a.py": "old-a\n", "b.py": "old-b\n"})
    base = _head(repo)
    (repo / "a.py").write_text("new-a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", "a already delivered"],
        check=True, capture_output=True,
    )
    (repo / "b.py").write_text("new-b\n", encoding="utf-8")
    diff = _replace_diff("a.py", "old-a", "new-a") + _replace_diff(
        "b.py", "old-b", "new-b"
    )

    def _fail_after_add(project_path, files, **_kwargs):
        subprocess.run(["git", "-C", project_path, "add", "--", *files], check=True)
        return {"ok": False, "committed": False, "reason": "forced"}

    monkeypatch.setattr("swarm.project.diff_apply.commit_task_output", _fail_after_add)

    out = deliver_merged_diff_locked(str(repo), diff, base, ["a.py", "b.py"], "t-mixed")

    assert out["finalization_error"] == "commit_failed"
    assert (repo / "a.py").read_text(encoding="utf-8") == "new-a\n"
    assert (repo / "b.py").read_text(encoding="utf-8") == "old-b\n"
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == ""


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Probe:
    returncode = 0
    stdout = ""
    stderr = ""


def _successful_git_probe(args, **_kwargs):
    probe = _Probe()
    if args[-1] == "--show-toplevel":
        probe.stdout = str(args[2]) + "\n"
    return probe


_DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old
+new
"""


def _init_repo(tmp_path, files: dict[str, str]):
    repo = tmp_path / "git-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t",
         "commit", "-m", "base"],
        check=True, capture_output=True,
    )
    return repo


def _head(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _replace_diff(path: str, before: str, after: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1 +1 @@\n-{before}\n+{after}\n"
    )
