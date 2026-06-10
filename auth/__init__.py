"""Swarm 多用户 RBAC。"""

from swarm.auth.passwords import generate_api_token, hash_password, verify_password
from swarm.auth.rbac import Role, can, effective_project_role, role_permissions
from swarm.auth.store import (
    SwarmUser,
    authenticate,
    create_user,
    ensure_auth_tables,
    ensure_bootstrap_admin,
    get_project_member_role,
    get_user_by_id,
    get_user_by_token,
    list_project_members,
    list_users,
    profile_key,
    set_project_member,
    user_can_on_project,
)

__all__ = [
    "Role",
    "SwarmUser",
    "authenticate",
    "can",
    "create_user",
    "effective_project_role",
    "ensure_auth_tables",
    "ensure_bootstrap_admin",
    "generate_api_token",
    "get_project_member_role",
    "get_user_by_id",
    "get_user_by_token",
    "hash_password",
    "list_project_members",
    "list_users",
    "profile_key",
    "role_permissions",
    "set_project_member",
    "user_can_on_project",
    "verify_password",
]
