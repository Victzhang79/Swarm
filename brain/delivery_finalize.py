"""真实项目树交付的同步 Git 临界区。"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

logger = logging.getLogger("swarm.brain.nodes")

def _safe_delivery_path(root: Path, rel_path: str) -> Path | None:
    rel = Path(str(rel_path))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    target = root / rel
    try:
        target.resolve(strict=False).relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return target


def _capture_paths(
    root: Path,
    rel_paths: list[str],
    snapshot: dict[str, tuple],
) -> list[str]:
    """保存工作树精确前像，供 Git/非 Git/子目录项目统一回滚。"""
    failed: list[str] = []
    for rel_path in dict.fromkeys(str(p) for p in rel_paths if p):
        if rel_path in snapshot:
            continue
        target = _safe_delivery_path(root, rel_path)
        if target is None:
            failed.append(rel_path)
            continue
        try:
            if target.is_symlink():
                snapshot[rel_path] = ("symlink", os.readlink(target))
            elif target.is_file():
                snapshot[rel_path] = (
                    "file", target.read_bytes(), stat.S_IMODE(target.stat().st_mode)
                )
            elif not target.exists():
                snapshot[rel_path] = ("missing",)
            else:
                failed.append(rel_path)
        except OSError:
            failed.append(rel_path)
    return failed


def _manifest_paths(root: Path) -> list[str]:
    """枚举 reconcile 真实可能写入的清单，不把叶包同名文件扩大成冲突面。"""
    from swarm.worker.workspace_manifest import workspace_manifest_write_candidates

    return workspace_manifest_write_candidates(root)


def _manifest_write_paths(
    root: Path,
    manifest_paths: list[str],
) -> tuple[list[str], dict[str, str], list[str]]:
    """展开清单真实写目标；仓外/损坏链接拒绝进入交付事务。"""
    root_resolved = root.resolve()
    expanded: list[str] = []
    targets: dict[str, str] = {}
    invalid: list[str] = []
    for rel_path in dict.fromkeys(manifest_paths):
        candidate = _safe_delivery_path(root, rel_path)
        if candidate is None:
            invalid.append(rel_path)
            continue
        target_rel = rel_path
        if candidate.is_symlink():
            try:
                resolved = candidate.resolve(strict=True)
                target_rel = resolved.relative_to(root_resolved).as_posix()
                if not resolved.is_file():
                    raise ValueError("清单链接目标不是文件")
            except (OSError, ValueError):
                invalid.append(rel_path)
                continue
        targets[rel_path] = target_rel
        expanded.extend((rel_path, target_rel))
    return list(dict.fromkeys(expanded)), targets, invalid


def _capture_git_index(root: Path) -> tuple[Path, bytes | None, int] | None:
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "index"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    index_path = Path(probe.stdout.strip())
    if not index_path.is_absolute():
        index_path = root / index_path
    try:
        if not index_path.exists():
            return index_path, None, 0
        return index_path, index_path.read_bytes(), stat.S_IMODE(index_path.stat().st_mode)
    except OSError:
        return None


def _restore_snapshot(
    root: Path,
    snapshot: dict[str, tuple],
    index_snapshot: tuple[Path, bytes | None, int] | None,
) -> list[str]:
    failed: list[str] = []
    for rel_path, before in snapshot.items():
        target = _safe_delivery_path(root, rel_path)
        if target is None:
            failed.append(rel_path)
            continue
        try:
            if before[0] == "missing":
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    failed.append(rel_path)
            elif before[0] == "symlink":
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        failed.append(rel_path)
                        continue
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(before[1])
            else:
                if target.is_symlink():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(before[1])
                target.chmod(before[2])
        except OSError:
            failed.append(rel_path)
    if index_snapshot is not None:
        index_path, content, mode = index_snapshot
        try:
            if content is None:
                if index_path.exists():
                    index_path.unlink()
            else:
                index_path.parent.mkdir(parents=True, exist_ok=True)
                index_path.write_bytes(content)
                index_path.chmod(mode)
        except OSError:
            failed.append("<git-index>")
    return failed


def deliver_merged_diff_locked(
    proj_path: str,
    merged_diff: str,
    base_commit: str | None,
    out_files: list[str],
    task_id: str | None,
) -> dict:
    """在项目级跨进程锁内原子执行 reset、apply、清单对账和 commit。"""
    from swarm.brain.integration_review import (
        _reset_worktree_to_head,
        reconcile_worktree_to_merged_diff,
        restore_worktree_to_diff_baseline,
        worktree_matches_merged_diff,
    )
    from swarm.git_base import files_changed_since_base, uncommitted_changed_files
    from swarm.project.diff_apply import (
        apply_git_diff_resilient,
        commit_task_output,
    )
    from swarm.worker.executor import _ProjectGitFlock

    result: dict = {"ap": {}, "wm": {}, "commit": {}, "out_files": list(out_files)}
    with _ProjectGitFlock(proj_path):
        root = Path(proj_path)
        manifest_paths, manifest_targets, unsafe_manifests = _manifest_write_paths(
            root, _manifest_paths(root)
        )
        if unsafe_manifests:
            result["ap"] = {
                "ok": False,
                "stage": "manifest_symlink_outside_project",
                "failed": unsafe_manifests,
                "reason": "聚合清单的真实写目标不在项目内或不可读取，拒绝写穿符号链接",
            }
            return result
        delivery_snapshot: dict[str, tuple] = {}
        snapshot_failed = _capture_paths(
            root, [*out_files, *manifest_paths], delivery_snapshot
        )
        if snapshot_failed:
            result["ap"] = {
                "ok": False,
                "stage": "delivery_snapshot_failed",
                "failed": snapshot_failed,
            }
            return result
        already_present = False
        reconciled_now = False
        git_probe = subprocess.run(
            ["git", "-C", proj_path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        head_probe = subprocess.run(
            ["git", "-C", proj_path, "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        inside_git = git_probe.returncode == 0
        is_git = inside_git and head_probe.returncode == 0
        index_snapshot = _capture_git_index(root)
        if inside_git and index_snapshot is None:
            result["ap"] = {
                "ok": False,
                "stage": "git_index_snapshot_failed",
                "failed": ["<git-index>"],
                "reason": "无法捕获 Git index 前像，拒绝在不可回滚状态下写入",
            }
            return result
        repo_root_project = (
            is_git
            and Path(git_probe.stdout.strip()).resolve() == Path(proj_path).resolve()
        )
        try:
            committed_conflicts = (
                files_changed_since_base(proj_path, base_commit, out_files, strict=True)
                if is_git and base_commit
                else []
            )
            dirty_files = (
                uncommitted_changed_files(proj_path, out_files, strict=True)
                if is_git
                else []
            )
            dirty_manifests = (
                uncommitted_changed_files(proj_path, manifest_paths, strict=True)
                if inside_git and manifest_paths
                else []
            )
        except RuntimeError as exc:
            result["ap"] = {
                "ok": False,
                "stage": "worktree_conflict_check_failed",
                "failed": list(out_files),
                "reason": str(exc),
            }
            return result
        output_paths = set(out_files)
        unrelated_dirty_manifests = [
            path for path in dirty_manifests if path not in output_paths
        ]
        if unrelated_dirty_manifests:
            result["ap"] = {
                "ok": False,
                "stage": "manifest_worktree_conflict",
                "failed": unrelated_dirty_manifests,
                "reason": "聚合清单含非本任务未提交改动，拒绝裹挟进交付 commit",
            }
            return result
        preserve_committed: set[str] = set()
        if committed_conflicts:
            already_present, mismatched = worktree_matches_merged_diff(
                proj_path, merged_diff, base_commit
            )
            if not already_present:
                result["ap"] = {
                    "ok": False,
                    "stage": "worktree_conflict",
                    "failed": mismatched or committed_conflicts,
                    "reason": "交付文件在任务基线后已有提交，拒绝覆盖",
                }
                return result
            preserve_committed = set(committed_conflicts)

        if dirty_files and not already_present:
            already_present, mismatched = worktree_matches_merged_diff(
                proj_path, merged_diff, base_commit
            )
            if not already_present:
                result["ap"] = {
                    "ok": False,
                    "stage": "worktree_conflict",
                    "failed": mismatched or dirty_files,
                    "reason": "交付文件含非任务补丁的未提交改动，拒绝覆盖",
                }
                return result

        if not repo_root_project:
            reconciled_ok, was_already_present, mismatched = (
                reconcile_worktree_to_merged_diff(proj_path, merged_diff, base_commit)
            )
            if not reconciled_ok:
                result["ap"] = {
                    "ok": False,
                    "stage": "worktree_conflict",
                    "failed": mismatched or list(out_files),
                    "reason": "项目级基线/期望树对账失败，拒绝覆盖第三种内容",
                }
                return result
            already_present = True
            reconciled_now = not was_already_present

        def _restore_index(*, to_current_head: bool = False) -> list[str]:
            if to_current_head:
                if not result["out_files"]:
                    return []
                reset_index = subprocess.run(
                    ["git", "-C", proj_path, "reset", "-q", "HEAD", "--",
                     *result["out_files"]],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if reset_index.returncode != 0:
                    logger.error(
                        "[LEARN_SUCCESS] HEAD 前移后无法把本任务路径 index 对齐新 HEAD: %s",
                        (reset_index.stderr or "")[:200],
                    )
                    return ["<git-index>"]
                return []
            return _restore_snapshot(root, {}, index_snapshot)

        def _rollback_outputs(*, index_to_current_head: bool = False) -> list[str]:
            rollback_files = [path for path in out_files if path not in preserve_committed]
            failed = restore_worktree_to_diff_baseline(
                proj_path, merged_diff, base_commit, rollback_files
            )
            return list(dict.fromkeys([
                *failed,
                *_restore_index(to_current_head=index_to_current_head),
            ]))

        reset_failed = (
            []
            if already_present
            else _reset_worktree_to_head(proj_path, merged_diff, base_commit)
        )
        if reset_failed:
            logger.warning(
                "[LEARN_SUCCESS] F5 交付前 reset 半失败 %d 文件 → fail-closed "
                "不 apply 不 commit（防交付混入旧残留）: %s",
                len(reset_failed),
                reset_failed[:8],
            )
            result["ap"] = {
                "ok": False,
                "stage": "reset_partial_failure",
                "failed": reset_failed,
            }
            return result
        if already_present:
            result["ap"] = {
                "ok": True,
                "stage": "reconciled" if reconciled_now else "already_present",
                "applied": list(out_files),
                "failed": [],
            }
        else:
            try:
                result["ap"] = apply_git_diff_resilient(proj_path, merged_diff)
            except Exception as exc:  # noqa: BLE001 — apply 可能已分文件落下一部分
                rollback_failed = _rollback_outputs()
                result["ap"] = {
                    "ok": False,
                    "stage": "apply_exception",
                    "applied": [],
                    "failed": list(out_files),
                    "rollback_failed": rollback_failed,
                    "reason": str(exc),
                }
                logger.warning(
                    "[LEARN_SUCCESS] resilient apply 中途异常，已在 commit 前终止；"
                    "回滚失败=%d: %s",
                    len(rollback_failed),
                    exc,
                )
                return result
        applied = list(result["ap"].get("applied") or [])
        apply_incomplete = (
            result["ap"].get("ok") is not True or bool(result["ap"].get("failed"))
        )
        if apply_incomplete:
            rollback_failed = _rollback_outputs()
            result["ap"] = {
                **result["ap"],
                "ok": False,
                "stage": (
                    "apply_partial_rollback_failed"
                    if rollback_failed
                    else "apply_partial_failure" if applied else "apply_failure"
                ),
                "rollback_failed": rollback_failed,
            }
            logger.warning(
                "[LEARN_SUCCESS] merged diff 未完整落盘（成功段=%d，失败段=%d，"
                "回滚失败=%d）→ reconcile/commit 前 fail-closed",
                len(applied),
                len(result["ap"].get("failed") or []),
                len(rollback_failed),
            )
            return result

        def _rollback_finalization(
            stage: str,
            *,
            index_to_current_head: bool = False,
        ) -> dict:
            """把文件与 index 精确恢复到进入临界区前，跨 Git 形态保持同一语义。"""
            rollback_failed = _rollback_outputs(
                index_to_current_head=index_to_current_head
            )
            manifest_snapshot = {
                path: before
                for path, before in delivery_snapshot.items()
                if path not in output_paths
            }
            rollback_failed.extend(
                _restore_snapshot(root, manifest_snapshot, None)
            )
            rollback_failed = list(dict.fromkeys(rollback_failed))
            result["finalization_error"] = stage
            result["ap"] = {
                **result["ap"],
                "ok": False,
                "stage": stage,
                "rollback_failed": rollback_failed,
            }
            return result

        try:
            from swarm.worker.workspace_manifest import reconcile_workspace_manifests

            manifest_result = reconcile_workspace_manifests(proj_path)
            result["wm"] = manifest_result
            modified_manifests = list(manifest_result.get("modified_manifests") or [])
            for manifest in modified_manifests:
                for commit_path in (manifest, manifest_targets.get(manifest, manifest)):
                    if commit_path not in result["out_files"]:
                        result["out_files"].append(commit_path)
            if manifest_result.get("reconcile_errors"):
                logger.error(
                    "[LEARN_SUCCESS] 清单对账存在子生态失败，拒绝 commit 并回滚: %s",
                    manifest_result["reconcile_errors"],
                )
                return _rollback_finalization("manifest_reconcile_failed")
        except Exception as exc:  # noqa: BLE001
            result["wm_error"] = str(exc)
            logger.error(
                "[LEARN_SUCCESS] 清单对账整体异常，拒绝 commit 并回滚: %s", exc
            )
            return _rollback_finalization("manifest_reconcile_failed")
        expected_commit_snapshot: dict[str, tuple] = {}
        commit_snapshot_failed = _capture_paths(
            root, result["out_files"], expected_commit_snapshot
        )
        if commit_snapshot_failed:
            logger.error(
                "[LEARN_SUCCESS] 无法捕获待提交任务树，拒绝在不可证状态下 commit: %s",
                commit_snapshot_failed,
            )
            return _rollback_finalization("commit_snapshot_failed")
        head_before_commit = head_probe.stdout.strip() if head_probe.returncode == 0 else ""
        try:
            result["commit"] = commit_task_output(
                proj_path, result["out_files"], task_id=task_id
            )
        except Exception as exc:  # noqa: BLE001 — 未来实现/测试替身未必内部吞异常
            result["commit"] = {
                "ok": False,
                "committed": False,
                "reason": str(exc),
            }
        if result["commit"].get("ok") is not True:
            head_after_probe = subprocess.run(
                ["git", "-C", proj_path, "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            head_after_commit = (
                head_after_probe.stdout.strip() if head_after_probe.returncode == 0 else ""
            )
            observed_commit_snapshot: dict[str, tuple] = {}
            observation_failed = _capture_paths(
                root, result["out_files"], observed_commit_snapshot
            )
            try:
                dirty_after_commit = uncommitted_changed_files(
                    proj_path, result["out_files"], strict=True
                )
            except RuntimeError as exc:
                observation_failed.append("<git-status>")
                dirty_after_commit = list(result["out_files"])
                logger.warning(
                    "[LEARN_SUCCESS] commit 失败后的任务树状态探针异常，拒绝恢复成功: %s",
                    exc,
                )
            task_tree_proven = (
                not observation_failed
                and not dirty_after_commit
                and observed_commit_snapshot == expected_commit_snapshot
            )
            if (
                head_after_commit
                and head_after_commit != head_before_commit
                and task_tree_proven
            ):
                logger.warning(
                    "[LEARN_SUCCESS] commit helper 返回失败但 HEAD 已前移，"
                    "且任务树逐字未变、目标路径已干净，按已提交事实收口: %s",
                    head_after_commit[:12],
                )
                result["commit"] = {
                    **result["commit"],
                    "ok": True,
                    "committed": True,
                    "commit_hash": head_after_commit[:12],
                    "observation_recovered": True,
                }
                return result
            head_moved = bool(
                head_after_commit and head_after_commit != head_before_commit
            )
            if head_moved:
                logger.warning(
                    "[LEARN_SUCCESS] HEAD 虽已前移，但无法证明新提交包含本任务产物；"
                    "脏路径=%s，观测失败=%s，拒绝误认领无关 commit",
                    dirty_after_commit,
                    observation_failed,
                )
            logger.error(
                "[LEARN_SUCCESS] 本地 commit 失败，拒绝保留未固化工作树并回滚: %s",
                result["commit"].get("reason"),
            )
            return _rollback_finalization(
                "commit_failed", index_to_current_head=head_moved
            )
    return result
