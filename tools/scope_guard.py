"""ScopeGuard — 文件访问权限检查，通过 context variable 全局注入

每个 Worker 启动时设置 current_scope，Tool 内部自动检查可读/可写权限。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

from swarm.types import FileScope

# ──────────────────────────────────────────────
# 全局 Context Variable — 每个 Worker 协程独立
# ──────────────────────────────────────────────
_current_scope: ContextVar[Optional[FileScope]] = ContextVar("current_scope", default=None)


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
        return self.scope.is_readable(path)

    def check_writable(self, path: str) -> bool:
        """检查路径是否在可写范围内"""
        return self.scope.is_writable(path)


# ──────────────────────────────────────────────
# 便捷函数 — Tool 内部直接调用
# ──────────────────────────────────────────────

def require_readable(path: str) -> str:
    """要求路径可读，否则返回错误消息（不抛异常，适合 Tool 返回值）

    Returns:
        空字符串表示通过，否则为错误消息
    """
    scope = get_scope()
    if scope.is_readable(path):
        return ""
    readable_list = ", ".join(scope.readable + scope.writable)
    return f"⛔ 权限拒绝：路径 '{path}' 不在可读范围内。可读文件：[{readable_list}]"


def require_writable(path: str) -> str:
    """要求路径可写，否则返回错误消息（不抛异常，适合 Tool 返回值）

    Returns:
        空字符串表示通过，否则为错误消息
    """
    scope = get_scope()
    if scope.is_writable(path):
        return ""
    writable_list = ", ".join(scope.writable)
    return f"⛔ 权限拒绝：路径 '{path}' 不在可写范围内。可写文件：[{writable_list}]"
