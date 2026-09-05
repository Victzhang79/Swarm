"""ScopeGuard — 文件访问权限检查，通过 context variable 全局注入

每个 Worker 启动时设置 current_scope，Tool 内部自动检查可读/可写权限。
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Optional

from swarm.types import FileScope, _path_scope_match

# ──────────────────────────────────────────────
# 全局 Context Variable — 每个 Worker 协程独立
# ──────────────────────────────────────────────
_current_scope: ContextVar[Optional[FileScope]] = ContextVar("current_scope", default=None)


def _canonical_scope_path(path: str) -> str | None:
    """把工具路径规范为 workspace 相对路径；无法证明归属时返回 ``None``。

    本地路径用 ``resolve`` 展开 ``..`` 与符号链接；沙箱绝对路径只接受配置的
    远端工作根前缀，并做纯 POSIX 词法归一。FileScope 因而无需尾部猜测。
    """
    original = str(path or "")
    if not original or original != original.strip() or "\x00" in original:
        return None
    # POSIX 上反斜杠是合法文件名、授权层却曾把它解释为分隔符，造成判权/IO 分叉。
    # 工具契约统一只接受 POSIX 路径；调用方必须先明确改写，不能隐式猜测。
    if "\\" in original:
        return None
    raw = original

    lexical = raw[:-1] if raw.endswith("/") and raw != "/" else raw
    parts = lexical.split("/")
    if raw.startswith("/"):
        parts = parts[1:]
    if lexical != "." and any(part in ("", ".", "..") for part in parts):
        return None

    from swarm.tools.paths import workspace_root

    try:
        root = Path(workspace_root()).resolve()
    except (OSError, RuntimeError, ValueError):
        return None

    candidate = Path(raw)
    if not candidate.is_absolute():
        try:
            return (root / candidate).resolve().relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None

    try:
        return candidate.resolve().relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        pass

    try:
        from swarm.config.settings import get_config

        remote_raw = str(get_config().sandbox.sandbox_remote_workdir or "/workspace")
        remote = PurePosixPath(os.path.normpath(remote_raw.replace("\\", "/")))
        normalized = PurePosixPath(os.path.normpath(raw))
        return normalized.relative_to(remote).as_posix()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _canonical_delete_path(path: str) -> str | None:
    """规范化删除目标，但保留最终目录项身份，不跟随叶节点符号链接。"""
    original = str(path or "")
    if (
        not original
        or original != original.strip()
        or "\x00" in original
        or "\\" in original
        or original.endswith("/")
    ):
        return None
    parts = original.split("/")
    if original.startswith("/"):
        parts = parts[1:]
    if any(part in ("", ".", "..") for part in parts):
        return None

    from swarm.tools.paths import workspace_root

    try:
        root = Path(workspace_root()).resolve()
    except (OSError, RuntimeError, ValueError):
        return None

    candidate = Path(original)
    if not candidate.is_absolute():
        try:
            parent_rel = (root / candidate.parent).resolve().relative_to(root)
            return (parent_rel / candidate.name).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None

    try:
        parent_rel = candidate.parent.resolve().relative_to(root)
        return (parent_rel / candidate.name).as_posix()
    except (OSError, RuntimeError, ValueError):
        pass

    try:
        from swarm.config.settings import get_config

        remote_raw = str(get_config().sandbox.sandbox_remote_workdir or "/workspace")
        remote = PurePosixPath(os.path.normpath(remote_raw.replace("\\", "/")))
        normalized = PurePosixPath(os.path.normpath(original))
        return normalized.relative_to(remote).as_posix()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def canonical_scope_path(path: str) -> str | None:
    """公开统一的 workspace realpath 规范化原语，供最终闸门复用。"""
    return _canonical_scope_path(path)


def _scope_allows(
    scope: FileScope,
    canonical: str,
    *,
    writable: bool,
    delete_only: bool = False,
) -> bool:
    """请求与授权项使用同一 realpath 语义比较，避免 symlink grant 假拒绝。"""
    if scope.allow_any:
        return True
    if delete_only:
        grants = scope.delete_files
    elif writable:
        grants = scope.writable + scope.create_files
    else:
        grants = scope.writable + scope.create_files + scope.delete_files + scope.readable
    for grant in grants:
        canonical_grant = (
            _canonical_delete_path(grant) if delete_only else _canonical_scope_path(grant)
        )
        if canonical_grant is None:
            continue
        if delete_only and canonical == canonical_grant:
            return True
        if not delete_only and (
            canonical_grant == "." or _path_scope_match(canonical, canonical_grant)
        ):
            return True
    if writable and not delete_only:
        for grant in scope.delete_files:
            canonical_grant = _canonical_delete_path(grant)
            if canonical_grant is not None and canonical == canonical_grant:
                return True
    return False


def authorized_read_path(path: str, scope: FileScope | None = None) -> str | None:
    """返回同时通过 workspace 归属与读权限的唯一相对路径。"""
    effective = scope or get_scope()
    canonical = _canonical_scope_path(path)
    if canonical is None or not _scope_allows(effective, canonical, writable=False):
        return None
    return canonical


def authorized_write_path(path: str, scope: FileScope | None = None) -> str | None:
    """返回同时通过 workspace 归属与写权限的唯一相对路径。"""
    effective = scope or get_scope()
    canonical = _canonical_scope_path(path)
    if canonical is None or not _scope_allows(effective, canonical, writable=True):
        return None
    return canonical


def authorized_delete_path(path: str, scope: FileScope | None = None) -> str | None:
    """仅允许 ``delete_files`` 明示的目标；allow_any 仍须位于 workspace 内。"""
    effective = scope or get_scope()
    canonical = _canonical_delete_path(path)
    if canonical is None or not _scope_allows(
        effective, canonical, writable=True, delete_only=True
    ):
        return None
    return canonical


def set_scope(scope: FileScope) -> None:
    """设置当前协程的文件访问 Scope（Worker 启动时调用）"""
    _current_scope.set(scope)


def get_scope() -> FileScope:
    """获取当前协程的文件访问 Scope

    Raises:
        RuntimeError: 如果未设置 scope
    """
    scope = _current_scope.get()
    if scope is None:
        raise RuntimeError("FileScope 未设置，请先调用 set_scope()")
    return scope


def clear_scope() -> None:
    """清除当前协程的 Scope（Worker 退出时调用）"""
    _current_scope.set(None)


class ScopeGuard:
    """Scope 感知的文件访问守卫

    使用方式一（直接实例化）:
        guard = ScopeGuard()
        if guard.check_readable(path):
            ...

    使用方式二（上下文管理器，自动设置/清理 scope）:
        with ScopeGuard(scope) as guard:
            guard.check_writable(path)

    使用方式三（全局 context variable，Tool 内部自动检查）:
        set_scope(scope)
        require_writable(path)   # 不通过则抛异常
        require_readable(path)
    """

    def __init__(self, scope: Optional[FileScope] = None):
        self._scope = scope
        self._token: Optional[object] = None

    def __enter__(self) -> ScopeGuard:
        if self._scope is not None:
            self._token = _current_scope.set(self._scope)
        return self

    def __exit__(self, *args: object) -> None:
        if self._token is not None:
            _current_scope.reset(self._token)  # type: ignore[attr-defined]

    @property
    def scope(self) -> FileScope:
        return self._scope if self._scope is not None else get_scope()

    def check_readable(self, path: str) -> bool:
        """检查路径是否在可读范围内"""
        return authorized_read_path(path, self.scope) is not None

    def check_writable(self, path: str) -> bool:
        """检查路径是否在可写范围内"""
        return authorized_write_path(path, self.scope) is not None


# ──────────────────────────────────────────────
# 便捷函数 — Tool 内部直接调用
# ──────────────────────────────────────────────

def require_readable(path: str) -> str:
    """要求路径可读，否则返回错误消息（不抛异常，适合 Tool 返回值）

    Returns:
        空字符串表示通过，否则为错误消息
    """
    scope = get_scope()
    if authorized_read_path(path, scope) is not None:
        return ""
    readable_list = ", ".join(scope.readable + scope.writable)
    return f"⛔ 权限拒绝：路径 '{path}' 不在可读范围内。可读文件：[{readable_list}]"


def require_writable(path: str) -> str:
    """要求路径可写，否则返回错误消息（不抛异常，适合 Tool 返回值）

    Returns:
        空字符串表示通过，否则为错误消息
    """
    scope = get_scope()
    if authorized_write_path(path, scope) is not None:
        return ""
    targets = getattr(scope, "all_write_targets", lambda: scope.writable)()
    writable_list = ", ".join(targets)
    return f"⛔ 权限拒绝：路径 '{path}' 不在可写范围内。可写文件（含新建/删除）：[{writable_list}]"
