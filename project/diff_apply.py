"""将 unified diff 应用到项目 git 工作区。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


def files_from_unified_diff(diff: str) -> list[str]:
    """从 unified diff 提取变更文件路径（去重，保持顺序）。"""
    seen: set[str] = set()
    paths: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path == "/dev/null":
                continue
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def snapshot_files(project_path: str, files: list[str]) -> dict[str, Any]:
    """在应用 diff 前，把受影响文件备份到临时目录，返回可回滚的快照句柄。

    回滚不依赖 git（greenfield/非 git 仓库也可用）。记录每个文件的原始内容
    或"原本不存在"标记，restore_snapshot 据此恢复或删除。
    """
    backup_dir = tempfile.mkdtemp(prefix="swarm_rollback_")
    root = Path(project_path)
    entries: dict[str, dict[str, Any]] = {}
    for rel in files:
        src = root / rel
        if src.is_file():
            dst = Path(backup_dir) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                entries[rel] = {"existed": True, "backup": str(dst)}
            except OSError:
                entries[rel] = {"existed": True, "backup": None}
        else:
            # 文件原本不存在（diff 新建）：回滚时应删除
            entries[rel] = {"existed": False, "backup": None}
    return {
        "project_path": project_path,
        "backup_dir": backup_dir,
        "entries": entries,
        "created_at": time.time(),
    }


def restore_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """回滚：用 snapshot_files 的快照恢复文件到应用 diff 之前的状态。"""
    if not snapshot or not snapshot.get("entries"):
        return {"ok": False, "reason": "empty snapshot"}
    root = Path(snapshot["project_path"])
    restored, deleted, failed = 0, 0, 0
    for rel, info in snapshot["entries"].items():
        target = root / rel
        try:
            if info["existed"] and info.get("backup"):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(info["backup"], target)
                restored += 1
            elif not info["existed"]:
                # 原本不存在 → 删除新建的文件
                if target.is_file():
                    target.unlink()
                deleted += 1
        except OSError:
            failed += 1
    return {"ok": failed == 0, "restored": restored, "deleted": deleted, "failed": failed}


def discard_snapshot(snapshot: dict[str, Any]) -> None:
    """应用确认成功后清理快照临时目录。"""
    bd = (snapshot or {}).get("backup_dir")
    if bd and os.path.isdir(bd):
        shutil.rmtree(bd, ignore_errors=True)


def apply_git_diff(
    project_path: str,
    diff: str,
    *,
    check_only: bool = False,
    backup_first: bool = False,
) -> dict[str, Any]:
    """在项目目录执行 git apply --check 或 git apply。

    backup_first=True 时，在真正 apply 前对受影响文件做快照，返回结果含
    'snapshot' 句柄；调用方可在后续验证失败时用 restore_snapshot 回滚，
    成功后用 discard_snapshot 清理。
    """
    if not diff.strip():
        return {"ok": False, "stage": "input", "stderr": "empty diff"}

    # 关键(task bce82e96)：git apply 要求 patch 文件【以换行结尾】，否则最后一行 hunk 被判
    # "corrupt patch at line N"（末行截断）。worker git diff 经 rstrip("\n") 后末尾无换行，
    # 这里补回一个 \n。用【bytes 模式】写，避免文本模式的 universal-newlines 改写 CRLF 的 \r。
    patch_bytes = diff.encode("utf-8")
    if not patch_bytes.endswith(b"\n"):
        patch_bytes += b"\n"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".patch", delete=False) as tf:
        tf.write(patch_bytes)
        patch_path = tf.name

    try:
        # --ignore-whitespace：关键(task 93159ec3)——RuoYi 等项目源文件是 CRLF，但 worker
        # 产出/归一化后的 diff 是 LF。不忽略空白(行尾)差异会让 git apply 因 context 行 CRLF↔LF
        # 不匹配而 "补丁未应用/损坏"。--ignore-whitespace 让行尾差异不阻断 apply，只比对真实内容。
        check = subprocess.run(
            ["git", "apply", "--check", "--ignore-whitespace", patch_path],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if check.returncode != 0:
            return {
                "ok": False,
                "stage": "check",
                "stdout": check.stdout,
                "stderr": check.stderr,
            }
        if check_only:
            return {"ok": True, "stage": "check", "message": "git apply --check 通过"}

        snapshot = None
        if backup_first:
            snapshot = snapshot_files(project_path, files_from_unified_diff(diff))

        applied = subprocess.run(
            ["git", "apply", "--ignore-whitespace", patch_path],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if applied.returncode != 0:
            # apply 失败：若已建快照则回滚到干净态
            if snapshot:
                restore_snapshot(snapshot)
                discard_snapshot(snapshot)
            return {
                "ok": False,
                "stage": "apply",
                "stdout": applied.stdout,
                "stderr": applied.stderr,
            }
        result: dict[str, Any] = {"ok": True, "stage": "apply", "message": "Diff 已应用到工作区"}
        if snapshot:
            result["snapshot"] = snapshot
        return result
    finally:
        try:
            os.unlink(patch_path)
        except OSError:
            pass
