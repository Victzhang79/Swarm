#!/usr/bin/env python3
"""RBAC — 用户、登录、项目成员与权限。"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.auth import rbac
from swarm.auth.passwords import hash_password, verify_password
from swarm.auth.store import (
    SwarmUser,
    authenticate,
    create_user,
    ensure_auth_tables,
    get_user_by_token,
    profile_key,
    set_project_member,
    user_can_on_project,
)


def test_password_hash_roundtrip():
    h = hash_password("secret")
    assert verify_password("secret", h)
    assert not verify_password("wrong", h)
    print("  ✅ password hash")


def test_rbac_role_permissions():
    assert rbac.can("admin", "task:approve")
    assert rbac.can("viewer", "project:read")
    assert not rbac.can("viewer", "task:create")
    print("  ✅ rbac permissions")


def test_profile_key():
    assert profile_key("u1", "p1") == "u1:p1"
    print("  ✅ profile_key")


def test_user_can_on_project_logic():
    try:
        ensure_auth_tables()
    except Exception as exc:
        print(f"  ⏭ user_can_on_project — PG unavailable: {exc}")
        return
    owner = SwarmUser("1", "o", "O", "developer")
    assert user_can_on_project(
        SwarmUser("a", "admin", "A", "admin"),
        "project:delete",
        "proj",
    )
    # 无成员的旧项目：登录 developer 可访问（legacy 兼容）
    assert user_can_on_project(owner, "project:read", "legacy_no_members_proj")
    # 有成员但未加入时不可访问
    other = create_user(username=f"other_{uuid.uuid4().hex[:6]}", password="x")
    blocked_pid = f"blocked_{uuid.uuid4().hex[:8]}"
    set_project_member(blocked_pid, other.id, "owner")
    assert not user_can_on_project(owner, "project:read", blocked_pid)
    print("  ✅ user_can_on_project")


def test_auth_store_crud():
    """需 PostgreSQL；不可用时跳过。"""
    try:
        ensure_auth_tables()
    except Exception as exc:
        print(f"  ⏭ auth store CRUD — PG unavailable: {exc}")
        return

    suffix = uuid.uuid4().hex[:8]
    username = f"test_{suffix}"
    user = create_user(username=username, password="pass123", global_role="developer")
    assert user.api_token

    authed = authenticate(username, "pass123")
    assert authed and authed.id == user.id

    by_token = get_user_by_token(user.api_token)
    assert by_token and by_token.username == username

    project_id = f"proj_{suffix}"
    set_project_member(project_id, user.id, "owner")
    assert user_can_on_project(user, "task:create", project_id)

    print("  ✅ auth store CRUD")


def main():
    print("=== test_rbac ===")
    test_password_hash_roundtrip()
    test_rbac_role_permissions()
    test_profile_key()
    test_user_can_on_project_logic()
    test_auth_store_crud()
    print("=== all passed ===")


if __name__ == "__main__":
    main()
