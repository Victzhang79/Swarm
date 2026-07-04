"""RBAC — 角色与权限定义。"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"
    OWNER = "owner"
    DEVELOPER = "developer"
    VIEWER = "viewer"


# 全局 admin 拥有全部权限
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.ADMIN.value: frozenset({"*"}),
    Role.OWNER.value: frozenset({
        "project:read", "project:write", "project:delete",
        "task:create", "task:read", "task:approve", "task:cancel",
        "worker:run", "knowledge:write", "memory:write", "config:write",
        "member:manage",
    }),
    Role.DEVELOPER.value: frozenset({
        "project:read", "project:create",
        "task:create", "task:read", "task:approve", "task:cancel",
        "worker:run", "knowledge:write", "memory:write", "memory:read",
    }),
    Role.VIEWER.value: frozenset({
        "project:read", "task:read", "memory:read",
    }),
}

# C1：项目级【可经成员接口分配】的角色白名单。排除 ADMIN——admin 是全局角色，其 "*" 通配符
# 不应经 PUT /members 授予（否则任意 owner 可把自己/他人提为项目级 admin 拿全权）。
PROJECT_ASSIGNABLE_ROLES: frozenset[str] = frozenset({
    Role.OWNER.value, Role.DEVELOPER.value, Role.VIEWER.value,
})


def role_permissions(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS[Role.VIEWER.value])


def can(role: str, permission: str) -> bool:
    perms = role_permissions(role)
    return "*" in perms or permission in perms


def effective_project_role(global_role: str, member_role: str | None) -> str:
    if global_role == Role.ADMIN.value:
        return Role.ADMIN.value
    if member_role:
        return member_role
    if global_role in ROLE_PERMISSIONS:
        return global_role
    return Role.VIEWER.value
