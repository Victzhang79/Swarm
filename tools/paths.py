"""工作区路径 — 优先使用 Brain runner 注入的 SWARM_WORKSPACE_ROOT"""

from __future__ import annotations

import os
from pathlib import Path

from swarm.config.settings import get_config


def workspace_root() -> Path:
    env = os.environ.get("SWARM_WORKSPACE_ROOT")
    if env:
        return Path(env)
    return get_config().workspace_root
