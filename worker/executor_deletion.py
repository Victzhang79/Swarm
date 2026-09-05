"""Worker 声明删除的 scope、远端探测与宿主传播边界。"""

from __future__ import annotations

import logging
from pathlib import Path

from swarm.config.settings import get_config
from swarm.models.errors import TransientInfraError
from swarm.worker.git_flock import _ProjectGitFlock

logger = logging.getLogger(__name__)


class _WorkerDeletionMixin:
    """把不可逆删除从通用 sandbox sync god-file 中隔离。"""

    def _delete_files(self) -> list[str]:
        out: list[str] = []
        for value in list(getattr(self.effective_scope, "delete_files", []) or []):
            rel = str(value).strip()
            if rel and rel not in out:
                out.append(rel)
        return out

    def _change_files(self) -> list[str]:
        return list(dict.fromkeys(self._writable_files() + self._delete_files()))

    def _missing_declared_deletions(self, local_root: Path) -> list[str]:
        """返回执行前存在、但产出时仍存在的声明删除目标。"""
        root = Path(local_root).resolve()
        missing: list[str] = []
        for value in self._delete_files():
            rel = self._norm_rel(root, value)
            baseline = self._delete_seed_snapshots.get(rel)
            if baseline is None:
                raise TransientInfraError(
                    f"declared deletion baseline missing for {rel}"
                )
            raw = Path(rel)
            if raw.is_absolute() or ".." in raw.parts:
                raise TransientInfraError(
                    f"declared deletion path invalid for {rel}"
                )
            parent = (root / raw.parent).resolve()
            try:
                parent.relative_to(root)
            except ValueError as exc:
                raise TransientInfraError(
                    f"declared deletion path escaped workspace for {rel}"
                ) from exc
            path = parent / raw.name
            try:
                current = self._worker_path_snapshot(path)
            except ValueError:
                missing.append(rel)
                continue
            if current != ("missing", None, None):
                missing.append(rel)
        return missing

    def _apply_local_deletions(self, local_root: Path, exists_in_sandbox) -> list[str]:
        """把沙箱内已完成的声明删除以 CAS 方式传播到共享工作树。"""
        deleted: list[str] = []
        raw_upload_errors = list(getattr(self, "_upload_error_rels", None) or [])
        upload_errors = {
            str(item).partition(":")[0].strip() for item in raw_upload_errors
        }
        upload_blocks = {
            str(item.get("path") or "")
            for item in (getattr(self, "_upload_blocked_rels", None) or [])
            if isinstance(item, dict)
        }
        if raw_upload_errors:
            raise TransientInfraError(
                "sandbox deletion seed completeness unknown: "
                + "; ".join(map(str, raw_upload_errors[:5]))
            )
        for value in getattr(self.effective_scope, "delete_files", []) or []:
            rel = self._norm_rel(local_root, value)
            if not rel:
                continue
            if rel in upload_errors or rel in upload_blocks:
                self._log(f"删除种子未完整上传，拒绝据沙箱缺席删除本地文件: {rel}")
                continue
            raw = Path(rel)
            try:
                parent = (local_root / raw.parent).resolve()
                parent.relative_to(local_root.resolve())
            except (OSError, ValueError):
                self._log(f"删除路径越界（不在项目根内），拒删: {rel}")
                continue
            local_path = parent / raw.name
            if exists_in_sandbox(rel):
                continue
            try:
                with _ProjectGitFlock(local_root):
                    expected = self._delete_seed_snapshots.get(rel)
                    if expected is None:
                        raise TransientInfraError(
                            f"sandbox deletion seed snapshot missing: {rel}"
                        )
                    current = self._worker_path_snapshot(local_path)
                    if (
                        rel in self._deleted_local_paths
                        and current == ("missing", None, None)
                    ):
                        continue
                    if current != expected:
                        raise TransientInfraError(
                            f"sandbox deletion concurrent change conflict: {rel}"
                        )
                    if local_path.is_file() or local_path.is_symlink():
                        local_path.unlink()
                        self._deleted_local_paths.add(rel)
                        deleted.append(rel)
            except TransientInfraError:
                raise
            except OSError as exc:
                logger.warning(
                    "删除传播失败 %s（保留本地，需核查权限/占用）: %s",
                    rel,
                    exc,
                    exc_info=True,
                )
                raise TransientInfraError(
                    f"sandbox deletion local unlink failed for {rel}: {exc}"
                ) from exc
        return deleted

    def _sandbox_file_exists(self, rel: str) -> bool:
        """逐文件精确探测；失败时拒绝把缺席解释为已删除。"""
        if not self._sandbox or not self._sandbox_manager:
            return True
        run_command = getattr(self._sandbox_manager, "run_command", None)
        if run_command is None:
            return True
        import shlex

        remote = get_config().sandbox.sandbox_remote_workdir
        quoted_path = shlex.quote(f"{remote}/{rel}")
        try:
            result = run_command(
                self._sandbox,
                f"test -e {quoted_path} || test -L {quoted_path}; "
                "if [ $? -eq 0 ]; then echo __Y__; else echo __N__; fi",
                timeout=15,
            )
            if getattr(result, "success", False) is not True or getattr(
                result, "error", None
            ):
                raise TransientInfraError(
                    f"sandbox delete probe failed for {rel}: "
                    f"{getattr(result, 'error', '')}"
                )
            markers = [
                line.strip()
                for line in (getattr(result, "stdout", "") or "").splitlines()
                if line.strip() in {"__Y__", "__N__"}
            ]
            if markers == ["__Y__"]:
                return True
            if markers == ["__N__"]:
                return False
            raise TransientInfraError(
                f"sandbox delete probe incomplete for {rel}: marker missing or ambiguous"
            )
        except TransientInfraError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransientInfraError(
                f"sandbox delete probe failed for {rel}: {exc}"
            ) from exc
