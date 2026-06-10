"""L0 会话元数据 —  ephemeral，绝不持久化。"""

from __future__ import annotations

import platform
import subprocess
from datetime import datetime, timezone
from typing import Any


def _git_branch(project_path: str | None) -> str | None:
    if not project_path:
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or None
    except Exception:
        pass
    return None


def build_session_metadata(
    *,
    project_path: str | None = None,
    client: str = "api",
) -> dict[str, Any]:
    """构建 L0 会话元数据（仅内存，写入 BrainState）。"""
    now = datetime.now(timezone.utc)
    local = datetime.now().astimezone()
    return {
        "client": client,
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "timezone": str(local.tzinfo or "UTC"),
        "local_time": local.isoformat(timespec="seconds"),
        "started_at_utc": now.isoformat(timespec="seconds"),
        "git_branch": _git_branch(project_path),
    }
