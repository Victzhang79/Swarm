"""将 unified diff 应用到项目 git 工作区。"""

from __future__ import annotations

import subprocess
import tempfile
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


def apply_git_diff(project_path: str, diff: str, *, check_only: bool = False) -> dict[str, Any]:
    """在项目目录执行 git apply --check 或 git apply。"""
    if not diff.strip():
        return {"ok": False, "stage": "input", "stderr": "empty diff"}

    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False, encoding="utf-8") as tf:
        tf.write(diff)
        patch_path = tf.name

    try:
        check = subprocess.run(
            ["git", "apply", "--check", patch_path],
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

        applied = subprocess.run(
            ["git", "apply", patch_path],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if applied.returncode != 0:
            return {
                "ok": False,
                "stage": "apply",
                "stdout": applied.stdout,
                "stderr": applied.stderr,
            }
        return {"ok": True, "stage": "apply", "message": "Diff 已应用到工作区"}
    finally:
        try:
            import os

            os.unlink(patch_path)
        except OSError:
            pass
