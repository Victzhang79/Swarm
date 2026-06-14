"""A2 三级沙箱可见性单测（升级版）。

三级模型：
- 系统管理员（global admin）：所有沙箱
- 项目管理员（项目 owner）：项目内所有沙箱
- 项目成员（developer/viewer）：仅自己创建任务的沙箱

需真 PG。PG 不可用则跳过。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _has_pg() -> bool:
    try:
        from swarm.auth.store import ensure_auth_tables
        ensure_auth_tables()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _has_pg(), reason="PG unavailable")


def _fake_request(user):
    return SimpleNamespace(state=SimpleNamespace(user=user), path_params={})


# ─── _can_see_sandbox 三级逻辑（纯判定，不依赖沙箱实例）───

def test_admin_sees_all():
    from swarm.api.routers.sandbox import _can_see_sandbox
    from swarm.auth.store import SwarmUser
    admin = SwarmUser("a", "admin", "A", "admin")
    assert _can_see_sandbox(admin, "any_proj", "someone_else") is True
    assert _can_see_sandbox(admin, None, None) is True  # 无归属也可见


def test_project_owner_sees_all_in_project():
    from swarm.api.routers.sandbox import _can_see_sandbox
    from swarm.auth.store import create_user, set_project_member
    suffix = uuid.uuid4().hex[:8]
    owner = create_user(username=f"_test_own_{suffix}", password="x", global_role="developer")
    pid = f"_test_p_{suffix}"
    set_project_member(pid, owner.id, "owner")
    # owner 能看项目内别人创建任务的沙箱
    assert _can_see_sandbox(owner, pid, "another_user_id") is True


def test_project_member_sees_only_own_tasks():
    from swarm.api.routers.sandbox import _can_see_sandbox
    from swarm.auth.store import create_user, set_project_member
    suffix = uuid.uuid4().hex[:8]
    member = create_user(username=f"_test_mem_{suffix}", password="x", global_role="developer")
    pid = f"_test_pm_{suffix}"
    set_project_member(pid, member.id, "developer")
    # 自建任务沙箱 → 可见
    assert _can_see_sandbox(member, pid, member.id) is True
    # 别人创建任务的沙箱（同项目）→ 不可见（成员只看自建）
    assert _can_see_sandbox(member, pid, "other_user_id") is False
    # 任务无创建者信息 → 不可见
    assert _can_see_sandbox(member, pid, None) is False


def test_non_member_sees_nothing():
    from swarm.api.routers.sandbox import _can_see_sandbox
    from swarm.auth.store import create_user
    suffix = uuid.uuid4().hex[:8]
    outsider = create_user(username=f"_test_out_{suffix}", password="x", global_role="developer")
    # 非该项目成员 → 任何沙箱都不可见
    assert _can_see_sandbox(outsider, f"_test_foreign_{suffix}", outsider.id) is False


def test_require_sandbox_access_denies_member_on_others_task(monkeypatch):
    """端点级：成员访问别人任务的沙箱 → 403。"""
    import swarm.api.routers.sandbox as sbx
    from swarm.auth.store import create_user, set_project_member
    suffix = uuid.uuid4().hex[:8]
    member = create_user(username=f"_test_d_{suffix}", password="x", global_role="developer")
    pid = f"_test_pd_{suffix}"
    set_project_member(pid, member.id, "developer")
    # mock 沙箱归属该项目、任务由别人创建
    monkeypatch.setattr(
        sbx, "_sandbox_owner_info", lambda mgr, sid: (pid, "task_x")
    )
    monkeypatch.setattr(sbx, "_task_creator", lambda tid: "someone_else")
    monkeypatch.setattr(sbx._app, "_get_sandbox_manager", lambda: SimpleNamespace())
    with pytest.raises(HTTPException) as ei:
        sbx._require_sandbox_access(_fake_request(member), "sid1")
    assert ei.value.status_code == 403


def test_require_sandbox_access_allows_member_own_task(monkeypatch):
    """端点级：成员访问自建任务的沙箱 → 放行。"""
    import swarm.api.routers.sandbox as sbx
    from swarm.auth.store import create_user, set_project_member
    suffix = uuid.uuid4().hex[:8]
    member = create_user(username=f"_test_d2_{suffix}", password="x", global_role="developer")
    pid = f"_test_pd2_{suffix}"
    set_project_member(pid, member.id, "developer")
    monkeypatch.setattr(sbx, "_sandbox_owner_info", lambda mgr, sid: (pid, "task_y"))
    monkeypatch.setattr(sbx, "_task_creator", lambda tid: member.id)
    monkeypatch.setattr(sbx._app, "_get_sandbox_manager", lambda: SimpleNamespace())
    u = sbx._require_sandbox_access(_fake_request(member), "sid1")
    assert u.id == member.id


def test_require_admin_rejects_non_admin():
    import swarm.api.routers.sandbox as sbx
    from swarm.auth.store import create_user
    suffix = uuid.uuid4().hex[:8]
    user = create_user(username=f"_test_na_{suffix}", password="x", global_role="developer")
    with pytest.raises(HTTPException) as ei:
        sbx._require_admin(_fake_request(user))
    assert ei.value.status_code == 403


# ─── 项目成员管理（A2 用户/角色闭环）───

def test_set_and_remove_project_member():
    """指派项目成员 → 角色生效 → 移除 → 角色消失。"""
    from swarm.auth.store import (
        create_user,
        get_project_member_role,
        remove_project_member,
        set_project_member,
    )
    suffix = uuid.uuid4().hex[:8]
    user = create_user(username=f"_test_mm_{suffix}", password="x", global_role="developer")
    pid = f"_test_mmproj_{suffix}"
    # 指派为项目管理员（owner）
    set_project_member(pid, user.id, "owner")
    assert get_project_member_role(pid, user.id) == "owner"
    # 改为成员
    set_project_member(pid, user.id, "developer")
    assert get_project_member_role(pid, user.id) == "developer"
    # 移除
    assert remove_project_member(pid, user.id) is True
    assert get_project_member_role(pid, user.id) is None
    # 重复移除返回 False
    assert remove_project_member(pid, user.id) is False


def test_member_manage_permission_by_role():
    """member:manage 权限：owner 有、developer/viewer 无（决定能否指派成员）。"""
    from swarm.auth.rbac import can
    assert can("admin", "member:manage") is True
    assert can("owner", "member:manage") is True
    assert can("developer", "member:manage") is False
    assert can("viewer", "member:manage") is False


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
