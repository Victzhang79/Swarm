"""A2 批1 单测：沙箱操作 RBAC 项目级 enforce。

验证 _require_sandbox_access / _require_admin：
- admin 全权
- 普通用户仅自己有权限项目的沙箱
- 无项目归属沙箱仅 admin
- 普通用户访问别人项目沙箱被拒（403）

需真 PG（RBAC 表）。PG 不可用则跳过。
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


def _patch_manager(monkeypatch, sandbox_pid):
    """让 _sandbox_project_id 返回指定 project_id。"""
    import swarm.api.routers.sandbox as sbx
    fake_mgr = SimpleNamespace(get_sandbox_meta=lambda sid: {"project_id": sandbox_pid} if sandbox_pid else {})
    monkeypatch.setattr(sbx._app, "_get_sandbox_manager", lambda: fake_mgr)
    return sbx


def test_admin_full_access(monkeypatch):
    from swarm.auth.store import SwarmUser
    sbx = _patch_manager(monkeypatch, "any_proj")
    admin = SwarmUser("a", "admin", "A", "admin")
    # admin 对任意项目沙箱放行
    u = sbx._require_sandbox_access(_fake_request(admin), "sid1", "task:read")
    assert u.global_role == "admin"


def test_member_access_own_project(monkeypatch):
    from swarm.auth.store import create_user, set_project_member
    sbx = _patch_manager(monkeypatch, None)
    suffix = uuid.uuid4().hex[:8]
    user = create_user(username=f"_test_m_{suffix}", password="x", global_role="developer")
    pid = f"_test_proj_{suffix}"
    set_project_member(pid, user.id, "developer")
    _patch_manager(monkeypatch, pid)  # 沙箱归属该项目
    u = sbx._require_sandbox_access(_fake_request(user), "sid1", "task:read")
    assert u.id == user.id


def test_member_denied_other_project(monkeypatch):
    from swarm.auth.store import create_user, set_project_member
    suffix = uuid.uuid4().hex[:8]
    user = create_user(username=f"_test_d_{suffix}", password="x", global_role="developer")
    # 沙箱归属【别人的】项目，user 非成员
    other_pid = f"_test_other_{suffix}"
    other = create_user(username=f"_test_o_{suffix}", password="x", global_role="developer")
    set_project_member(other_pid, other.id, "owner")
    sbx = _patch_manager(monkeypatch, other_pid)
    with pytest.raises(HTTPException) as ei:
        sbx._require_sandbox_access(_fake_request(user), "sid1", "task:read")
    assert ei.value.status_code == 403


def test_unowned_sandbox_admin_only(monkeypatch):
    from swarm.auth.store import SwarmUser, create_user
    sbx = _patch_manager(monkeypatch, None)  # 无归属
    # 让回退的服务端查询也返回空（无 swarm_project 标签）
    monkeypatch.setattr(sbx._app, "_fetch_sandbox_list_from_server", lambda: [])
    suffix = uuid.uuid4().hex[:8]
    user = create_user(username=f"_test_u_{suffix}", password="x", global_role="developer")
    # 普通用户对无归属沙箱被拒
    with pytest.raises(HTTPException) as ei:
        sbx._require_sandbox_access(_fake_request(user), "sid_orphan", "task:read")
    assert ei.value.status_code == 403
    # admin 可
    admin = SwarmUser("a", "admin", "A", "admin")
    u = sbx._require_sandbox_access(_fake_request(admin), "sid_orphan", "task:read")
    assert u.global_role == "admin"


def test_require_admin_rejects_non_admin(monkeypatch):
    from swarm.auth.store import create_user
    import swarm.api.routers.sandbox as sbx
    suffix = uuid.uuid4().hex[:8]
    user = create_user(username=f"_test_na_{suffix}", password="x", global_role="developer")
    with pytest.raises(HTTPException) as ei:
        sbx._require_admin(_fake_request(user))
    assert ei.value.status_code == 403


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
