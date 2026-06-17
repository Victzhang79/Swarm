"""工作区路径 — 优先使用 Brain runner 注入的工作区上下文（ContextVar，并发隔离）。"""

from __future__ import annotations

import contextvars
import os
from pathlib import Path

from swarm.config.settings import get_config

# M2 修复：原用进程级 os.environ["SWARM_WORKSPACE_ROOT"]，Brain 单进程并发跑不同项目时
# 互相覆盖工作根 → worker 本地 subprocess 打到错误项目。改 ContextVar 按 asyncio task 隔离。
# 保留 os.environ 读取作回退（子进程/外部注入兼容）。
_workspace_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "swarm_workspace_root", default=None)


def set_workspace_root(path: str | None) -> None:
    """设置当前任务的工作区根（按 asyncio task 隔离）。"""
    _workspace_var.set(path)
    # 同步写 os.environ 兼容：本地 subprocess 子进程通过环境变量继承工作根。
    # 注意：os.environ 是进程级、并发会串——但 ContextVar 优先读，os.environ 仅作子进程传递兜底。
    if path:
        os.environ["SWARM_WORKSPACE_ROOT"] = path


def workspace_root() -> Path:
    # ContextVar 优先（并发隔离），其次 os.environ（子进程/外部注入），最后配置默认。
    ctx = _workspace_var.get()
    if ctx:
        return Path(ctx)
    env = os.environ.get("SWARM_WORKSPACE_ROOT")
    if env:
        return Path(env)
    return get_config().workspace_root
