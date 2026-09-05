"""Worker 产物 provenance、CAS 变换、失败回滚与删除补丁混入。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from swarm.models.errors import TransientInfraError
from swarm.worker.git_flock import _ProjectGitFlock

logger = logging.getLogger(__name__)


class _WorkerProvenanceMixin:
    """把产物所有权与回滚簇从 sandbox sync 传输层拆出。"""

    def _remember_worker_discovered_baseline(self, rel: str) -> None:
        if rel in self._worker_discovered_baselines:
            return
        root = Path(self.project_path).resolve()
        raw = Path(str(rel))
        if raw.is_absolute() or ".." in raw.parts:
            raise ValueError(f"非法 Worker 发现路径: {rel}")
        parent = (root / raw.parent).resolve()
        parent.relative_to(root)
        self._worker_discovered_baselines[rel] = self._worker_path_snapshot(
            parent / raw.name
        )

    def _snapshot_declared_delete_seeds(self, local_root: Path) -> None:
        """在 Worker 动手前保存声明删除叶子的精确恢复基线。"""
        root = Path(local_root).resolve()
        self._delete_seed_snapshots = {}
        for delete_path in self._delete_files():
            rel = self._norm_rel(root, delete_path)
            if not rel:
                continue
            raw = Path(rel)
            if raw.is_absolute() or ".." in raw.parts:
                raise ValueError(f"非法声明删除路径: {delete_path}")
            parent = (root / raw.parent).resolve()
            parent.relative_to(root)
            self._delete_seed_snapshots[rel] = self._worker_path_snapshot(
                parent / raw.name
            )

    def _capture_local_tool_deletions(self) -> None:
        """从生命周期 tracker 收割已落宿主的 delete_file 副作用。"""
        from swarm.tools.inflight import current_tool_inflight_tracker

        tracker = current_tool_inflight_tracker()
        if tracker is not None:
            self._deleted_local_paths.update(tracker.consume_local_deletions())

    @staticmethod
    def _worker_path_snapshot(
        path: Path,
    ) -> tuple[str, bytes | str | None, int | None]:
        if path.is_symlink():
            return "symlink", os.readlink(path), None
        if path.is_file():
            return "file", path.read_bytes(), path.stat().st_mode & 0o7777
        if path.exists():
            raise ValueError(f"Worker 发现路径不是文件: {path}")
        return "missing", None, None

    def _record_worker_discovered_outputs(
        self,
        written_snapshots: dict[
            str, tuple[str, bytes | str | None, int | None]
        ],
    ) -> None:
        for rel in sorted(self._worker_discovered_paths):
            snapshot = written_snapshots.get(rel)
            if snapshot is None:
                raise TransientInfraError(
                    f"Worker pull-back provenance missing for discovered path: {rel}"
                )
            self._worker_discovered_outputs[rel] = snapshot

    def _expected_worker_discovered_snapshots(
        self,
    ) -> dict[str, tuple[str, bytes | str | None, int | None]]:
        return {
            rel: self._worker_discovered_outputs.get(rel, baseline)
            for rel, baseline in self._worker_discovered_baselines.items()
            if rel in self._worker_discovered_paths
        }

    def _expected_pullback_snapshots(
        self,
    ) -> dict[str, tuple[str, bytes | str | None, int | None]]:
        expected = {
            **getattr(self, "_bootstrap_entry_snapshots", {}),
            **getattr(self, "_pullback_written_snapshots", {}),
            **{
                rel: snapshot
                for rel, snapshot in self._expected_worker_discovered_snapshots().items()
                if rel not in getattr(self, "_pullback_written_snapshots", {})
            },
        }
        bootstrap = getattr(self, "_bootstrap_entry_snapshots", {})
        for rel in getattr(self, "_repaired_extra_paths", set()):
            if rel not in expected and rel in bootstrap:
                expected[rel] = bootstrap[rel]
        return expected

    def _cas_transform_local_file(
        self,
        local_root: Path,
        rel: str,
        transform,
    ) -> tuple[bytes, object]:
        from swarm.worker.sandbox import _atomic_write_bytes

        root = Path(local_root).resolve()
        raw = Path(rel)
        if raw.is_absolute() or ".." in raw.parts:
            raise ValueError(f"非法 Worker 路径: {rel}")
        parent = (root / raw.parent).resolve()
        parent.relative_to(root)
        path = parent / raw.name
        with _ProjectGitFlock(root):
            current = self._worker_path_snapshot(path)
            written = getattr(self, "_pullback_written_snapshots", {})
            expected = written.get(rel)
            if expected is not None:
                if current != expected:
                    raise TransientInfraError(
                        f"Worker post-transform concurrent change conflict: {rel}"
                    )
            elif rel in self._worker_discovered_paths:
                expected = self._worker_discovered_outputs.get(rel)
                if expected is None:
                    raise TransientInfraError(
                        f"Worker post-transform provenance missing: {rel}"
                    )
                if current != expected:
                    raise TransientInfraError(
                        f"Worker post-transform concurrent change conflict: {rel}"
                    )
            kind, value, mode = current
            if kind != "file" or not isinstance(value, bytes):
                raise TransientInfraError(
                    f"Worker post-transform target is not a regular file: {rel}"
                )
            new_data, metadata = transform(value)
            if not isinstance(new_data, bytes):
                raise TypeError("post-transform 必须返回 bytes")
            if new_data != value:
                _atomic_write_bytes(path, new_data)
                if mode is not None:
                    path.chmod(mode)
            snapshot = ("file", new_data, mode)
            written[rel] = snapshot
            self._pullback_written_snapshots = written
            if rel in self._worker_discovered_paths:
                self._worker_discovered_outputs[rel] = snapshot
            return new_data, metadata

    def _rollback_worker_discovered_paths(self) -> list[str]:
        from swarm.worker.sandbox import _atomic_write_bytes

        root = Path(self.project_path).resolve()
        errors: list[str] = []
        resolved: set[str] = set()
        restored_count = 0
        for rel in sorted(self._worker_discovered_paths):
            baseline = self._worker_discovered_baselines.get(rel)
            worker_output = self._worker_discovered_outputs.get(rel)
            if baseline is None or worker_output is None:
                errors.append(f"{rel}: 缺少 pull-back 前基线")
                continue
            try:
                with _ProjectGitFlock(root):
                    raw = Path(rel)
                    if raw.is_absolute() or ".." in raw.parts:
                        raise ValueError("非法相对路径")
                    parent = (root / raw.parent).resolve()
                    parent.relative_to(root)
                    path = parent / raw.name
                    current = self._worker_path_snapshot(path)
                    if current != worker_output:
                        errors.append(
                            f"[CONCURRENT_CHANGE] {rel}: 当前内容已不等于本 Worker 产出，"
                            "保留并发写入，拒绝覆盖"
                        )
                        resolved.add(rel)
                        continue
                    kind, value, mode = baseline
                    if path.exists() and path.is_dir() and not path.is_symlink():
                        raise IsADirectoryError(path)
                    if path.exists() or path.is_symlink():
                        path.unlink()
                    if kind == "file":
                        path.parent.mkdir(parents=True, exist_ok=True)
                        _atomic_write_bytes(
                            path, value if isinstance(value, bytes) else b""
                        )
                        if mode is not None:
                            path.chmod(mode)
                    elif kind == "symlink":
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.symlink_to(str(value))
                    elif kind != "missing":
                        raise ValueError(f"未知基线类型: {kind}")
                    resolved.add(rel)
                    restored_count += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")
        self._worker_discovered_paths.difference_update(resolved)
        for rel in resolved:
            self._worker_discovered_baselines.pop(rel, None)
            self._worker_discovered_outputs.pop(rel, None)
        if resolved:
            self._log(
                f"失败产出回滚：已恢复 {restored_count} 个 Worker 未声明文件，"
                f"保留 {len(resolved) - restored_count} 个并发写入"
            )
        if errors:
            logger.warning("Worker 未声明产物回滚不完整: %s", errors[:5])
        return errors

    def _rollback_declared_deletions(self) -> list[str]:
        """失败/取消时用执行前快照恢复本 Worker 已落到共享树的声明删除。"""
        from swarm.worker.sandbox import _atomic_write_bytes

        root = Path(self.project_path).resolve()
        errors: list[str] = []
        resolved: set[str] = set()
        restored = 0
        for rel in sorted(self._deleted_local_paths):
            baseline = self._delete_seed_snapshots.get(rel)
            if baseline is None:
                errors.append(f"{rel}: 缺少声明删除执行前基线")
                continue
            try:
                raw = Path(rel)
                if raw.is_absolute() or ".." in raw.parts:
                    raise ValueError("非法相对路径")
                parent = (root / raw.parent).resolve()
                parent.relative_to(root)
                path = parent / raw.name
                with _ProjectGitFlock(root):
                    current = self._worker_path_snapshot(path)
                    if current != ("missing", None, None):
                        errors.append(
                            f"[CONCURRENT_CHANGE] {rel}: 删除后路径已被重新创建，"
                            "保留并发写入，拒绝覆盖"
                        )
                        resolved.add(rel)
                        continue
                    kind, value, mode = baseline
                    if kind == "file":
                        path.parent.mkdir(parents=True, exist_ok=True)
                        _atomic_write_bytes(
                            path, value if isinstance(value, bytes) else b""
                        )
                        if mode is not None:
                            path.chmod(mode)
                        restored += 1
                    elif kind == "symlink":
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.symlink_to(str(value))
                        restored += 1
                    elif kind != "missing":
                        raise ValueError(f"未知基线类型: {kind}")
                    resolved.add(rel)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")
        self._deleted_local_paths.difference_update(resolved)
        if resolved:
            self._log(
                f"失败删除回滚：已恢复 {restored} 个声明删除，"
                f"保留 {len(resolved) - restored} 个并发重建"
            )
        if errors:
            logger.warning("Worker 声明删除回滚不完整: %s", errors[:5])
        return errors

    def _rollback_failed_worker_paths(self) -> list[str]:
        """统一回滚失败 Worker 的越界产物与已落地声明删除。"""
        return (
            self._rollback_worker_discovered_paths()
            + self._rollback_declared_deletions()
        )

    def _deletion_patch_from_bytes(self, rel: str, data: bytes) -> str:
        import subprocess
        import tempfile

        try:
            with tempfile.NamedTemporaryFile() as baseline:
                baseline.write(data)
                baseline.flush()
                result = subprocess.run(
                    [
                        "git", "diff", "--no-index", "--binary", "--",
                        baseline.name, "/dev/null",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
            return self._rewrite_deletion_patch(rel, result)
        except Exception as exc:  # noqa: BLE001
            self._log(f"生成删除补丁失败 {rel}: {exc}", level="warning")
            return ""

    def _deletion_patch_from_symlink(self, rel: str, target: str) -> str:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory) / "link"
            baseline.symlink_to(target)
            return self._deletion_patch_from_path(rel, baseline)

    def _deletion_patch_from_path(self, rel: str, baseline: Path) -> str:
        import subprocess

        result = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", str(baseline), "/dev/null"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return self._rewrite_deletion_patch(rel, result)

    def _rewrite_deletion_patch(self, rel: str, result) -> str:
        if result.returncode != 1 or not result.stdout.strip():
            self._log(
                f"无法为删除生成 patch: {rel}: {result.stderr.strip()}",
                level="warning",
            )
            return ""
        lines = result.stdout.splitlines()
        if lines and lines[0].startswith("diff --git "):
            lines[0] = f"diff --git a/{rel} b/{rel}"
        for idx, line in enumerate(lines):
            if line.startswith("--- "):
                lines[idx] = f"--- a/{rel}"
            elif line.startswith("+++ "):
                lines[idx] = "+++ /dev/null"
        return "\n".join(lines) + "\n"
